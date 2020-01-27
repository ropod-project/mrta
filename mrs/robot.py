import argparse
import logging.config

from fmlib.models.tasks import TaskStatus
from pymodm.context_managers import switch_collection
from pymodm.errors import DoesNotExist
from ropod.structs.status import ActionStatus, TaskStatus as TaskStatusConst

from mrs.config.configurator import Configurator
from mrs.db.models.task import Task
from mrs.exceptions.execution import InconsistentSchedule
from mrs.execution.delay_recovery import DelayRecovery
from mrs.execution.executor import Executor
from mrs.execution.schedule_monitor import ScheduleMonitor
from mrs.messages.assignment_update import AssignmentUpdate
from mrs.messages.dispatch_queue_update import DispatchQueueUpdate
from mrs.messages.task_status import TaskStatus as TaskStatusMessage
from mrs.simulation.simulator import SimulatorInterface, Simulator
from mrs.timetable.timetable import Timetable

_component_modules = {'simulator': Simulator,
                      'timetable': Timetable,
                      'executor': Executor,
                      'schedule_monitor': ScheduleMonitor,
                      'delay_recovery': DelayRecovery}


class Robot:
    def __init__(self, robot_id, api, robot_store, executor, schedule_monitor, **kwargs):

        self.robot_id = robot_id
        self.api = api
        self.robot_store = robot_store
        self.executor = executor
        self.schedule_monitor = schedule_monitor

        self.timetable = schedule_monitor.timetable
        self.timetable.fetch()
        self.simulator_interface = SimulatorInterface(kwargs.get('simulator'))

        self.assignments = list()
        self.queue_update_received = False

        self.api.register_callbacks(self)
        self.logger = logging.getLogger('mrs.robot.%s' % robot_id)
        self.logger.info("Initialized Robot %s", robot_id)

    def dispatch_queue_update_cb(self, msg):
        payload = msg['payload']
        self.logger.critical("Received dispatch queue update")
        d_queue_update = DispatchQueueUpdate.from_payload(payload)
        if self.recovery_method.startswith("re-schedule"):
            d_queue_update.update_timetable(self.timetable, replace=True)
        else:
            d_queue_update.update_timetable(self.timetable, replace=False)

        self.logger.debug("STN update %s", self.timetable.stn)
        self.logger.debug("Dispatchable graph update %s", self.timetable.dispatchable_graph)
        self.queue_update_received = True

    def task_cb(self, msg):
        payload = msg['payload']
        task = Task.from_payload(payload)
        if self.robot_id in task.assigned_robots:
            self.logger.critical("Received task %s", task.task_id)
            task.update_status(TaskStatusConst.DISPATCHED)
            task.freeze()

    def send_task_status(self, task):
        try:
            task_status = task.status
        except DoesNotExist:
            with switch_collection(Task, Task.Meta.archive_collection):
                with switch_collection(TaskStatus, TaskStatus.Meta.archive_collection):
                    task_status = TaskStatus.objects.get({"_id": task.task_id})

        self.logger.debug("Send task status of task %s", task.task_id)
        task_status = TaskStatusMessage(task.task_id, self.robot_id, task_status.status, task_status.delayed)
        msg = self.api.create_message(task_status)
        self.api.publish(msg)

    def send_assignment_update(self):
        self.logger.critical("Send AssignmentUpdate msg")
        assignment_update = AssignmentUpdate(self.robot_id, self.assignments)
        msg = self.api.create_message(assignment_update)
        self.api.publish(msg)
        self.assignments = list()

    @property
    def recovery_method(self):
        return self.schedule_monitor.recovery_method.name

    def re_allocate(self, task):
        self.logger.debug("Trigger re-allocation of task %s", task.task_id)
        task.update_status(TaskStatusConst.UNALLOCATED)
        self.timetable.remove_task(task.task_id)
        self.send_task_status(task)

    def abort(self, task):
        task.update_status(TaskStatusConst.ABORTED)
        self.timetable.remove_task(task.task_id)
        self.send_task_status(task)

    def schedule(self, task):
        try:
            assignment = self.schedule_monitor.schedule(task)
            self.assignments.append(assignment)
        except InconsistentSchedule:
            self.re_allocate(task)

    def start_execution(self, task):
        self.executor.start_execution(task)

    def continue_execution(self, task):
        cur_action = task.status.progress.current_action
        cur_action_prog = task.status.progress.get_action(cur_action.action_id)

        if (cur_action_prog.status == ActionStatus.ONGOING) or\
                (cur_action_prog.status == ActionStatus.PLANNED and not self.recovery_method.startswith("re-schedule"))\
                or \
                (cur_action_prog.status == ActionStatus.PLANNED and self.recovery_method.startswith("re-schedule")
                 and self.queue_update_received):

            self.queue_update_received = False
            assignment = self.executor.execute_action(task, cur_action)
            self.assignments.append(assignment)

            if self.schedule_monitor.recover(task, assignment):
                self.logger.debug("Applying recovery method: %s", self.recovery_method)
                self.recover(task)

        elif cur_action_prog.status == ActionStatus.COMPLETED:
            self.logger.debug("Completing execution of task %s", task.task_id)
            task.update_status(TaskStatusConst.COMPLETED)
            self.timetable.remove_task(task.task_id)
            self.send_task_status(task)

    def recover(self, task):
        if self.recovery_method == "re-allocate":
            task.mark_as_delayed()
            self.send_task_status(task)
            next_task = self.timetable.get_next_task(task)
            self.re_allocate(next_task)

        elif self.recovery_method.startswith("re-schedule"):
            self.send_assignment_update()

        elif self.recovery_method == "abort":
            next_task = self.timetable.get_next_task(task)
            self.abort(next_task)

    def run(self):
        try:
            self.api.start()
            while True:
                self.simulator_interface.run()
                try:
                    tasks = Task.get_tasks_by_robot(self.robot_id)
                    for task in tasks:
                        if task.status.status == TaskStatusConst.DISPATCHED and self.queue_update_received:
                            self.schedule(task)
                        if task.status.status == TaskStatusConst.SCHEDULED and self.executor.is_executable(
                                task.start_time):
                            self.start_execution(task)
                        if task.status.status == TaskStatusConst.ONGOING:
                            self.continue_execution(task)
                except DoesNotExist:
                    pass

                self.api.run()
        except (KeyboardInterrupt, SystemExit):
            self.logger.info("Terminating %s robot ...", self.robot_id)
            self.api.shutdown()
            self.logger.info("Exiting...")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, action='store', help='Path to the config file')
    parser.add_argument('robot_id', type=str, help='example: robot_001')
    args = parser.parse_args()

    config = Configurator(args.file, component_modules=_component_modules)
    components = config.config_robot(args.robot_id)

    robot = Robot(**components)
    robot.run()
