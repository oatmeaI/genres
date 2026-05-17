from time import time


class Timer:
    timers = {}

    def time(self, mark):
        if mark not in self.timers:
            self.timers[mark] = time()
        else:
            start_time = self.timers[mark]
            end_time = time()
            print("%r took %2.4fs" % (mark, end_time - start_time))
            del self.timers[mark]


timer = Timer()

