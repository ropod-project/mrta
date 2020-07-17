from mrs.utils.as_dict import AsDictMixin


class DGraphUpdate(AsDictMixin):

    def __init__(self, ztp, stn, dispatchable_graph, **kwargs):
        self.ztp = ztp
        self.stn = stn
        self.dispatchable_graph = dispatchable_graph

    def __eq__(self, other):
        if other is None:
            return False
        return (self.stn == other.stn and
                self.dispatchable_graph == other.dispatchable_graph)

    def __ne__(self, other):
        return not self.__eq__(other)

    def update_timetable(self, timetable, replace=True):
        stn_cls = timetable.stp_solver.get_stn()
        stn = stn_cls.from_dict(self.stn)
        dispatchable_graph = stn_cls.from_dict(self.dispatchable_graph)
        timetable.ztp = self.ztp
        if replace:
            timetable.stn = stn
            timetable.dispatchable_graph = dispatchable_graph
        else:
            merged_stn = self.merge_temporal_graph(timetable.stn, stn)
            merged_dispatchable_graph = self.merge_temporal_graph(timetable.dispatchable_graph, dispatchable_graph)
            timetable.stn = merged_stn
            timetable.dispatchable_graph = merged_dispatchable_graph

        timetable.store()

    @property
    def meta_model(self):
        return "d-graph-update"
