

class Scheduler(object):

    def __init__(self, robot_id, ccu_store, stp):
        self.id = robot_id
        self.ccu_store = ccu_store
        self.stp = stp

        self.task_scheduled = False
        self.navigation_start_time = -1  # of scheduled task

        self.schedule = stp.get_stn()

    def schedule_task(self, task, timetable):
        print("Time table:", timetable.dispatchable_graph)

        navigation_start = timetable.dispatchable_graph.get_task_navigation_start_time(task.id)
        self.schedule.add_task(task, 1)
        self.schedule.update_edge_weight(0, 1, navigation_start)
        self.schedule.update_edge_weight(1, 0, -navigation_start)

        self.task_scheduled = True

        timetable.schedule = self.schedule
        print("Schedule: ", timetable.schedule)
        self.ccu_store.update_timetable(timetable)

    def reset_schedule(self):
        self.schedule = self.stp.get_stn()
        self.task_scheduled = False

