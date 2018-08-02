#!/usr/bin/env python

import os
import imp
import logging

from flask import Flask, Response, jsonify, request, json
from multiprocessing import Process, Queue
import prometheus_client as prom

mod = imp.load_source('function',
                      '/kubeless/%s.py' % os.getenv('MOD_NAME'))
func = getattr(mod, os.getenv('FUNC_HANDLER'))
func_port = os.getenv('FUNC_PORT', 8080)

timeout = float(os.getenv('FUNC_TIMEOUT', 180))

log = logging.getLogger(__name__)
app = Flask(__name__)

func_hist = prom.Histogram('function_duration_seconds',
                           'Duration of user function in seconds',
                           ['method'])
func_calls = prom.Counter('function_calls_total',
                           'Number of calls to user function',
                          ['method'])
func_errors = prom.Counter('function_failures_total',
                           'Number of exceptions in user function',
                           ['method'])

function_context = {
    'function-name': func,
    'timeout': timeout,
    'runtime': os.getenv('FUNC_RUNTIME'),
    'memory-limit': os.getenv('FUNC_MEMORY_LIMIT'),
}


def funcWrap(q, event, c):
    try:
        q.put(func(event, c))
    except Exception as inst:
        q.put(inst)


def merge_two_dicts(x, y):
    z = x.copy()   # start with x's keys and values
    z.update(y)    # modifies z with y's keys and values & returns None
    return z


@app.route('/', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def handler():
    req = request
    req.get_data()
    content_type = req.headers.get('content-type')
    data = req.data
    if content_type == 'application/json':
        data = req.json
    event = {
        'data': data,
        'event-id': req.headers.get('event-id'),
        'event-type': req.headers.get('event-type'),
        'event-time': req.headers.get('event-time'),
        'event-namespace': req.headers.get('event-namespace'),
        'extensions': {
            'request': req
        }
    }
    method = req.method
    func_calls.labels(method).inc()
    with func_errors.labels(method).count_exceptions():
        with func_hist.labels(method).time():
            q = Queue()
            p = Process(target=funcWrap, args=(q, event, function_context))
            p.start()
            p.join(timeout)
            # If thread is still active
            if p.is_alive():
                p.terminate()
                p.join()
                return "Timeout while processing the function", 408
            else:
                res = q.get()
                call = {
                    "headers": dict(req.headers.items()),
                    "method": method,
                    "body": data
                }
                if isinstance(res, Exception):
                    raise res
                if 'error' in res:
                    return jsonify(merge_two_dicts(res, call)), 400
                return jsonify(res)


@app.route('/healthz', methods=['GET'])
def healthz():
    return 'OK', 200


@app.route('/metrics', methods=['GET'])
def metrics():
    return Response(prom.generate_latest(prom.REGISTRY), mimetype=prom.CONTENT_TYPE_LATEST)


if __name__ == '__main__':
    import logging
    import sys
    import requestlogger
    loggedapp = requestlogger.WSGILogger(
        app,
        [logging.StreamHandler(stream=sys.stdout)],
        requestlogger.ApacheFormatter())
    app.run(host='0.0.0.0', port=int(func_port))

