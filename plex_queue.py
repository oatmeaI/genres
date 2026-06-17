import time
import threading


class PlexRequestQueue:
    requests = []
    request_names = []
    running = False

    def __init__(self):
        self.start_timer()

    def start_timer(self):
        self.timer = threading.Timer(1, self.run_queue)
        self.timer.start()

    def queue_request(self, request, name):
        print("[que]", name)

        self.requests.append(request)
        self.request_names.append(name)

        if not self.timer.is_alive():
            self.start_timer()

        while request in self.requests:
            time.sleep(1)

    def run_queue(self):
        if len(self.requests) > 0:
            request = self.requests[0]
            name = self.request_names[0]
            print("[run]", name)
            request()
            print("[fin]", name)
            self.requests.remove(request)
            self.request_names.remove(name)

        if len(self.requests) > 0:
            self.start_timer()


plex_queue = PlexRequestQueue()
