from fmlib.models.tasks import TransportationTask as Task, TaskManager
from pymodm.context_managers import switch_collection
from stn.exceptions.stp import NodeNotFound

from mrs.timetable.timetable import Timetable


class TransportationTask(Task):
    objects = TaskManager()

    @property
    def start_time(self):
        if self.assigned_robots:
            # Gets the timetable of the first robot in the list of assigned robots
            # Does not work for single-task multi-robot
            robot_id = self.assigned_robots[0]
            timetable = Timetable.get_timetable(robot_id)
            try:
                start_time = timetable.get_start_time(self.task_id)
                return start_time.to_datetime()
            except NodeNotFound:
                return self._start_time
        return self._start_time

    @property
    def finish_time(self):
        if self.assigned_robots:
            # Gets the timetable of the first robot in the list of assigned robots
            # Does not work for single-task multi-robot
            robot_id = self.assigned_robots[0]
            timetable = Timetable.get_timetable(robot_id)
            try:
                finish_time = timetable.get_delivery_time(self.task_id)
                return finish_time.to_datetime()
            except NodeNotFound:
                return self._finish_time
        return self._finish_time

    def update_schedule(self):
        self._start_time = self.start_time
        self._finish_time = self.finish_time
        self.save()

    def archive(self):
        with switch_collection(TransportationTask, Task.Meta.archive_collection):
            super().save()
        self.delete()
