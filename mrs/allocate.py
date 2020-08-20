import logging.config
import time

from fmlib.db.mongo import MongoStore
from fmlib.db.mongo import MongoStoreInterface
from fmlib.models.tasks import TaskStatus
from fmlib.models.tasks import TransportationTask as Task
from pymodm.context_managers import switch_collection
from ropod.pyre_communicator.base_class import RopodPyre
from ropod.structs.task import TaskStatus as TaskStatusConst

from mrs.messages.task_contract import TaskContract
from mrs.simulation.simulator import Simulator, SimulatorInterface
from mrs.utils.datasets import load_tasks_to_db
from mrs.utils.utils import get_msg_fixture


class Allocate(RopodPyre):
    """ Zyre Node that triggers the allocation of a dataset

    Args:
        config_params (dict): Configuration parameters
        robot_poses (dict): Robot poses
        dataset_module (str): Name of the module that contains the dataset, e.g., ``mrs.tests.datasets``
        dataset_name (str): Name of the dataset, e.g., ``non_overlapping``

    Attributes:
        _config_params (dict): Configuration parameters
        _robot_poses (dict): Robot poses
        _dataset_module (str): Name of the module that contains the dataset, e.g., ``mrs.tests.datasets``
        _dataset_name (str): Name of the dataset, e.g., ``non_overlapping``
        logger (obj): Logger object
        simulator_interface (obj): Exposes methods to interact with the simulator
        allocations (dict): Stores allocations {task_id: robot_id, ...}
        terminated (bool): True for terminated test, False otherwise.
        tasks (list): Tasks in the dataset, referenced to an initial time

    """

    def __init__(self, config_params, robot_poses, dataset_module,
                 dataset_name):
        zyre_config = {
            'node_name': 'allocation_test',
            'groups': ['TASK-ALLOCATION'],
            'message_types': ['START-TEST', 'ALLOCATION']
        }

        super().__init__(zyre_config, acknowledge=False)

        self._config_params = config_params
        self._robot_poses = robot_poses
        self._dataset_module = dataset_module
        self._dataset_name = dataset_name

        self.logger = logging.getLogger('mrs.allocate')
        logger_config = self._config_params.get('logger')
        logging.config.dictConfig(logger_config)

        simulator = Simulator(**self._config_params.get("simulator"))
        self.simulator_interface = SimulatorInterface(simulator)

        self.allocations = dict()
        self.terminated = False
        self.clean_stores()
        self.tasks = self.load_tasks()

    def clean_store(self, store):
        """ Deletes all contents from the given database

        Args:
            store (str): Name of the database

        """
        store_interface = MongoStoreInterface(store)
        store_interface.clean()
        self.logger.info("Store %s cleaned", store_interface._store.db_name)

    def clean_stores(self):
        """ Cleans the databases: ccu_store, robot_proxy stores and robot stores
        """
        fleet = self._config_params.get('fleet')
        robot_proxy_store_config = self._config_params.get("robot_proxy_store")
        robot_store_config = self._config_params.get("robot_store")
        store_configs = {
            'robot_proxy_store': robot_proxy_store_config,
            'robot_store': robot_store_config
        }

        for robot_id in fleet:
            for store_name, config in store_configs.items():
                config.update(
                    {'db_name': store_name + '_' + robot_id.split('_')[1]})
                store = MongoStore(**config)
                self.clean_store(store)

        ccu_store_config = self._config_params.get('ccu_store')
        store = MongoStore(**ccu_store_config)
        self.clean_store(store)

    def send_robot_positions(self):
        """ Shouts ``robot-pose`` messages, one per robot_id in the fleet
        """
        msg = get_msg_fixture('robot_pose.json')
        fleet = self._config_params.get("fleet")
        for robot_id in fleet:
            pose = self._robot_poses.get(robot_id)
            msg['payload']['robotId'] = robot_id
            msg['payload']['pose'] = pose
            self.shout(msg)
            self.logger.info("Send init pose to %s: ", robot_id)

    def load_tasks(self):
        """ Loads the dataset ``_dataset_name`` in ``_dataset_module`` to the ccu_store. The tasks in the datasets are
        referenced tasks to an ``initial_time``:

            * In case the simulator is configured, the initial time is indicated in the config file.
            * If the simulator is not configured, the initial time is now.

        Returns:
            list: Tasks referenced to an initial time

        """
        sim_config = self._config_params.get('simulator')
        if sim_config:
            tasks = load_tasks_to_db(
                self._dataset_module,
                self._dataset_name,
                initial_time=sim_config.get('initial_time'))
        else:
            tasks = load_tasks_to_db(self._dataset_module, self._dataset_name)
        return tasks

    def trigger(self):
        """ Shouts a ``start-test`` message
        """
        msg = get_msg_fixture('start_test.json')
        self.shout(msg)
        self.logger.info("Test triggered")
        self.simulator_interface.start(msg["payload"]["initial_time"])

    def receive_msg_cb(self, msg_content):
        """ Receives messages and filters them by type.
        Messages from interest:
            * ``task-contract``

        Args:
            msg_content: message in json format

        """
        msg = self.convert_zyre_msg_to_dict(msg_content)
        if msg is None:
            return
        msg_type = msg['header']['type']
        payload = msg['payload']

        if msg_type == 'TASK-CONTRACT':
            task_contract = TaskContract.from_payload(payload)
            self.update_allocations(task_contract)

    def update_allocations(self, task_contract):
        """ Update the dict of ``allocations`` based on a task_contract

        Args:
            task_contract(obj): TaskContract object with task-contract msg info

        """
        self.allocations[task_contract.task_id] = task_contract.robot_id
        self.logger.debug("Allocation: (%s, %s)", task_contract.task_id,
                          task_contract.robot_id)

    def check_termination_test(self):
        """ Reads tasks' status from the ccu_store and sets ``terminated`` to ``True`` when the termination condition
        is met.

        Termination condition: the number of completed plus the number of preempted tasks is equal
        to the number of tasks in the dataset

            tasks  = completed_tasks + preempted_tasks

        """
        unallocated_tasks = Task.get_tasks_by_status(TaskStatusConst.UNALLOCATED)
        allocated_tasks = Task.get_tasks_by_status(TaskStatusConst.ALLOCATED)
        planned_tasks = Task.get_tasks_by_status(TaskStatusConst.PLANNED)
        dispatched_tasks = Task.get_tasks_by_status(TaskStatusConst.DISPATCHED)
        ongoing_tasks = Task.get_tasks_by_status(TaskStatusConst.ONGOING)

        with switch_collection(TaskStatus, TaskStatus.Meta.archive_collection):
            completed_tasks = Task.get_tasks_by_status(TaskStatusConst.COMPLETED)
            preempted_tasks = Task.get_tasks_by_status(TaskStatusConst.PREEMPTED)
            canceled_tasks = Task.get_tasks_by_status(TaskStatusConst.CANCELED)
            aborted_tasks = Task.get_tasks_by_status(TaskStatusConst.ABORTED)

        self.logger.info("Unallocated: %s", len(unallocated_tasks))
        self.logger.info("Allocated: %s", len(allocated_tasks))
        self.logger.info("Planned: %s ", len(planned_tasks))
        self.logger.info("Dispatched: %s", len(dispatched_tasks))
        self.logger.info("Ongoing: %s", len(ongoing_tasks))
        self.logger.info("Completed: %s ", len(completed_tasks))
        self.logger.info("Preempted: %s ", len(preempted_tasks))
        self.logger.info("Canceled: %s", len(canceled_tasks))
        self.logger.info("Aborted: %s", len(aborted_tasks))

        tasks = completed_tasks + preempted_tasks

        if len(tasks) == len(self.tasks):
            self.logger.info("Terminating test")
            self.logger.info("Allocations: %s", self.allocations)
            self.terminated = True

    def terminate(self):
        """ Terminates test: stops the simulator and shut downs the Pyre node.
        """
        print("Exiting test...")
        self.simulator_interface.stop()
        self.shutdown()
        print("Test terminated")

    def start_allocation(self):
        """ Triggers the allocation progress.

            * Starts the Pyre node
            * Sends robot poses
            * Triggers the test

        """
        self.start()
        time.sleep(10)
        self.send_robot_positions()
        time.sleep(10)
        self.trigger()

    def run(self):
        """ Starts the allocation and terminates when the termination condition is met.
        """
        try:
            self.start_allocation()
            while not self.terminated:
                print("Approx current time: ",
                      self.simulator_interface.get_current_time())
                self.check_termination_test()
                time.sleep(0.5)
            self.terminate()

        except (KeyboardInterrupt, SystemExit):
            print('Task request test interrupted; exiting')
            self.terminate()
