import copy
import logging
import time
from datetime import timedelta

from mrs.exceptions.allocation import AlternativeTimeSlot
from mrs.exceptions.allocation import NoAllocation
from mrs.bidding.bid import Bid
from ropod.utils.timestamp import TimeStamp
from ropod.utils.uuid import generate_uuid
from mrs.db.models.task import TaskLot


class Round(object):

    def __init__(self,  n_allocated_tasks, round_time=timedelta(seconds=5), **kwargs):

        self.logger = logging.getLogger('mrs.auctioneer.round')
        self.n_allocated_tasks = n_allocated_tasks
        self.round_time = round_time
        self.alternative_timeslots = kwargs.get('alternative_timeslots', False)

        self.closure_time = 0
        self.id = generate_uuid()
        self.finished = True
        self.opened = False
        self.received_bids = dict()
        self.received_no_bids = dict()
        self.start_time = time.time()
        self.time_to_allocate = None

    def start(self):
        """ Starts and auction round:
        - opens the round
        - marks the round as not finished

        opened: The auctioneer processes bid msgs
        closed: The auctioneer no longer processes incoming bid msgs, i.e.,
                bid msgs received after the round has closed are not
                considered in the election process

        After the round closes, the election process takes place

        finished: The election process is over, i.e., an mrs has been made
                    (or an exception has been raised)

        """
        open_time = TimeStamp()
        self.closure_time = TimeStamp(delta=self.round_time)
        self.logger.debug("Round opened at %s and will close at %s",
                          open_time, self.closure_time)

        self.finished = False
        self.opened = True

    def process_bid(self, payload):
        bid = Bid.from_payload(payload)

        self.logger.debug("Processing bid from robot %s: (risk metric: %s, temporal metric: %s)",
                          bid.robot_id, bid.risk_metric, bid.temporal_metric)

        if bid.cost != (None, None):
            # Process a bid
            if bid.task_id not in self.received_bids or \
                    self.update_task_bid(bid, self.received_bids[bid.task_id]):

                self.received_bids[bid.task_id] = bid

        else:
            # Process a no-bid
            self.received_no_bids[bid.task_id] = self.received_no_bids.get(bid.task_id, 0) + 1

    @staticmethod
    def update_task_bid(new_bid, old_bid):
        """ Called when more than one bid is received for the same task

        :return: boolean
        """
        old_robot_id = int(old_bid.robot_id.split('_')[-1])
        new_robot_id = int(new_bid.robot_id.split('_')[-1])

        if new_bid < old_bid or (new_bid == old_bid and new_robot_id < old_robot_id):
            return True

        return False

    def time_to_close(self):
        current_time = TimeStamp()

        if current_time < self.closure_time:
            return False

        self.logger.debug("Closing round at %s", current_time)
        self.time_to_allocate = time.time() - self.start_time
        self.opened = False
        return True

    def get_result(self):
        """ Returns the results of the mrs as a tuple

        :return: round_result

        task, robot_id, position, tasks_to_allocate = round_result

        task (obj): task allocated in this round
        robot_id (string): id of the winning robot
        position (int): position in the STN where the task was added
        tasks_to_allocate (dict): tasks left to allocate

        """
        # Check for which tasks the constraints need to be set to soft
        if self.alternative_timeslots and self.received_no_bids:
            self.set_soft_constraints()

        try:
            winning_bid = self.elect_winner()
            round_result = (winning_bid, self.time_to_allocate)

            if winning_bid.alternative_start_time:
                raise AlternativeTimeSlot(winning_bid, self.time_to_allocate)

            return round_result

        except NoAllocation:
            self.logger.warning("No allocation made in round %s ", self.id)
            raise NoAllocation(self.id)

    def finish(self):
        self.finished = True
        self.logger.debug("Round finished")

    def set_soft_constraints(self):
        """ If there are no bids for the task, set its temporal constraints to soft
        """
        for task_id, n_no_bids in self.received_no_bids.items():
            if task_id not in self.received_bids:
                task = TaskLot.get_task(task_id)
                task.set_soft_constraints()
                self.logger.debug("Setting soft constraints for task %s", task_id)

    def elect_winner(self):
        """ Elects the winner of the round

        :return:
        mrs(dict): key - task_id,
                          value - list of robots assigned to the task

        """
        lowest_bid = None

        for task_id, bid in self.received_bids.items():
            if lowest_bid is None \
                    or bid < lowest_bid \
                    or (bid == lowest_bid and bid.task_id < lowest_bid.task_id):
                lowest_bid = copy.deepcopy(bid)

        if lowest_bid is None:
            raise NoAllocation(self.id)

        return lowest_bid

