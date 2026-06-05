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


def form_bool(request, name):
    return request.form.get(name) in (
        "1",
        "on",
        "true",
        "yes",
    )


def query_bool(request, name):
    return request.args.get(name) in (
        "1",
        "on",
        "true",
        "yes",
    )


def disable_requests_logging():
    import logging
    import http.client as http_client

    http_client.HTTPConnection.debuglevel = 0
    # logging.basicConfig()
    # logging.getLogger().setLevel(logging.DEBUG)
    # requests_log = logging.getLogger("requests.packages.urllib3")
    # requests_log.setLevel(logging.DEBUG)
    # requests_log.propagate = True


def enable_requests_logging():
    import logging
    import http.client as http_client

    http_client.HTTPConnection.debuglevel = 1
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True
