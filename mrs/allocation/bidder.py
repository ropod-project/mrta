import copy
import logging

from fmlib.models.robot import Robot
from pymodm.errors import DoesNotExist
from ropod.structs.task import TaskStatus as TaskStatusConst
from stn.exceptions.stp import NoSTPSolution

from mrs.allocation.bidding_rule import BiddingRule
from mrs.db.models.task import InterTimepointConstraint
from mrs.db.models.task import Task
from mrs.messages.bid import Bid
from mrs.messages.task_announcement import TaskAnnouncement
from mrs.messages.task_contract import TaskContract
from mrs.messages.task_contract_acknowledgement import TaskContractAcknowledgment

""" Implements a variation of the the TeSSI algorithm using the bidding_rule
specified in the config file
"""


class Bidder:

    def __init__(self, robot_id, stp_solver, timetable, bidding_rule, auctioneer_name, **kwargs):
        """
        Includes bidder functionality for a robot in a multi-robot task-allocation auction-based
        approach

        Args:

            robot_id (str): id of the robot, e.g. robot_001
            stp_solver (STP): Simple Temporal Problem object
            bidding_rule(dict): robustness and temporal criteria for the bidding rule
            auctioneer_name (str): name of the auctioneer pyre node
            kwargs:
                api (API): object that provides middleware functionality
                robot_store (robot_store): interface to interact with the db

        """
        self.robot_id = robot_id
        self.stp_solver = stp_solver
        self.timetable = timetable
        self.api = kwargs.get('api')
        self.ccu_store = kwargs.get('ccu_store')
        self.planner = kwargs.get('planner')

        self.logger = logging.getLogger('mrs.bidder.%s' % self.robot_id)
        self.logger.debug("Initial timetable %s", self.timetable.stn)

        robustness = bidding_rule.get('robustness')
        temporal = bidding_rule.get('temporal')
        self.bidding_rule = BiddingRule(temporal)

        self.auctioneer_name = auctioneer_name
        self.bid_placed = None

        self.logger.debug("Bidder initialized %s", self.robot_id)

    def configure(self, **kwargs):
        api = kwargs.get('api')
        ccu_store = kwargs.get('ccu_store')
        if api:
            self.api = api
        if ccu_store:
            self.ccu_store = ccu_store

    def task_announcement_cb(self, msg):
        payload = msg['payload']
        task_announcement = TaskAnnouncement.from_payload(payload)
        self.logger.debug("Robot %s received TASK-ANNOUNCEMENT msg", self.robot_id)
        self.timetable.fetch()
        self.timetable.zero_timepoint = task_announcement.zero_timepoint
        self.compute_bids(task_announcement)

    def task_contract_cb(self, msg):
        self.logger.debug("Robot %s received TASK-CONTRACT", self.robot_id)
        payload = msg['payload']
        task_contract = TaskContract.from_payload(payload)

        if task_contract.robot_id == self.robot_id:
            self.allocate_to_robot(task_contract.task_id)
            self.send_contract_acknowledgement()

    def compute_bids(self, task_announcement):
        bids = list()
        no_bids = list()
        round_id = task_announcement.round_id

        for task in task_announcement.tasks:
            self.logger.debug("Computing bid of task %s", task.task_id)

            # Insert task in each possible insertion_point of the stn and
            # get the best_bid for each task
            best_bid = self.insert_task(task, round_id)

            if best_bid:
                self.logger.debug("Best bid for task %s: (risk metric: %s, temporal metric: %s)", task.task_id,
                                  best_bid.risk_metric, best_bid.temporal_metric)

                bids.append(best_bid)
            else:
                self.logger.warning("No bid for task %s", task.task_id)
                no_bid = Bid(self.robot_id, round_id, task.task_id)
                no_bids.append(no_bid)

        smallest_bid = self.get_smallest_bid(bids)

        self.send_bids(smallest_bid, no_bids)

    def send_bids(self, bid, no_bids):
        """ Sends the bid with the smallest cost
        Sends a no-bid per task that could not be accommodated in the stn

        :param bid: bid with the smallest cost
        :param no_bids: list of no bids
        """
        if bid:
            self.bid_placed = bid
            self.logger.debug("Placing bid %s ", self.bid_placed)
            self.send_bid(bid)

        if no_bids:
            for no_bid in no_bids:
                self.logger.debug("Sending no bid for task %s", no_bid.task_id)
                self.send_bid(no_bid)

    def insert_task(self, task, round_id):
        best_bid = None
        n_tasks = len(self.timetable.get_tasks())

        # Add task to the STN from insertion_point 1 onwards (insertion_point 0 is reserved for the zero_timepoint)
        for insertion_point in range(1, n_tasks+2):
            if not self.insert_in(insertion_point):
                continue

            self.logger.debug("Computing bid for task %s in insertion_point %s", task.task_id, insertion_point)
            previous_position = self.get_previous_position(insertion_point)
            travel_time = self.compute_travel_time(previous_position, task)

            try:
                bid = self.bidding_rule.compute_bid(self.robot_id, round_id, task, insertion_point, self.timetable,
                                                    travel_time)

                self.logger.debug("Bid: (risk metric: %s, temporal metric: %s)", bid.risk_metric, bid.temporal_metric)

                if best_bid is None or \
                        bid < best_bid or\
                        (bid == best_bid and bid.task_id < best_bid.task_id):

                    best_bid = bid

            except NoSTPSolution:
                self.logger.debug("The STN is inconsistent with task %s in insertion_point %s", task.task_id, insertion_point)

        return best_bid

    def insert_in(self, insertion_point):
        task = self.timetable.get_task(insertion_point)
        if task and task.status.status != TaskStatusConst.ALLOCATED:
            self.logger.debug("Not adding task in insertion_point %s", insertion_point)
            return False
        return True

    def get_previous_position(self, insertion_point):
        if insertion_point == 1:
            try:
                position = Robot.get_robot(self.robot_id).position
                previous_position = self.planner.get_node(position.x, position.y, position.theta)
            except DoesNotExist:
                self.logger.warning("No information about robot's current position")
                previous_position = "AMK_D_L-1_C39"
        else:
            previous_task = self.timetable.get_task(insertion_point-1)
            previous_position = previous_task.request.delivery_location

        self.logger.debug("Previous position: %s ", previous_position)
        return previous_position

    def compute_travel_time(self, previous_position, task):
        if self.planner:
            plan = self.planner.get_path(previous_position, task.request.pickup_location)
            mean, variance = self.planner.get_estimated_duration(plan)
        else:
            mean = 1
            variance = 0.1

        travel_time = InterTimepointConstraint(name="travel_time", mean=mean, variance=variance)
        self.logger.debug("Travel time: %s", travel_time)
        task.update_inter_timepoint_constraint("travel_time", mean, variance)
        return travel_time

    @staticmethod
    def get_smallest_bid(bids):
        """ Get the bid with the smallest cost among all bids.

        :param bids: list of bids
        :return: the bid with the smallest cost
        """
        smallest_bid = None

        for bid in bids:
            if smallest_bid is None or\
                    bid < smallest_bid or\
                    (bid == smallest_bid and bid.task_id < smallest_bid.task_id):

                smallest_bid = copy.deepcopy(bid)

        return smallest_bid

    def send_bid(self, bid):
        """ Creates bid_msg and sends it to the auctioneer
        """
        msg = self.api.create_message(bid)

        self.api.publish(msg, peer=self.auctioneer_name)

    def allocate_to_robot(self, task_id):

        # TODO: Refactor timetable update
        self.timetable.stn = self.bid_placed.stn
        self.timetable.dispatchable_graph = self.bid_placed.dispatchable_graph

        self.timetable.store()

        self.logger.debug("Robot %s allocated task %s", self.robot_id, task_id)
        self.logger.debug("STN %s", self.timetable.stn)
        self.logger.debug("Dispatchable graph %s", self.timetable.dispatchable_graph)

        tasks = [task for task in self.timetable.get_tasks()]

        self.logger.debug("Tasks allocated to robot %s:%s", self.robot_id, tasks)
        task = Task.get_task(task_id)
        task.update_status(TaskStatusConst.ALLOCATED)
        task.assign_robots([self.robot_id])

    def send_contract_acknowledgement(self):
        task_contract_acknowledgement = TaskContractAcknowledgment(self.robot_id)
        msg = self.api.create_message(task_contract_acknowledgement)

        self.logger.debug("Robot %s sends task-contract-acknowledgement msg ", self.robot_id)
        self.api.publish(msg, groups=['TASK-ALLOCATION'])

    def archive_task(self, robot_id, task_id, node_id):
        self.logger.debug("Deleting task %s", task_id)
        self.timetable.remove_task(node_id)
        self.logger.debug("STN robot %s: %s", robot_id, self.timetable.stn)
        self.logger.debug("Dispatchable graph robot %s: %s", robot_id, self.timetable.dispatchable_graph)
        # task = Task.get_task(task_id)
        # task.update_status(TaskStatusConst.COMPLETED)
