import json
import threading

from app.client import request
from app.server import make_server


def main():
    with make_server() as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        print(json.dumps(request(base, '/health'), ensure_ascii=False))
        for case in request(base, '/cases')['cases']:
            response = request(base, '/preview', {'case': case})
            print(json.dumps({'case': case, 'preview': response}, ensure_ascii=False))
        for case in request(base, '/study-cases')['cases']:
            response = request(base, '/study-preview', {'case': case})
            print(json.dumps({'study': case, 'preview': response}, ensure_ascii=False))
        # 新分析能力：真实 HTTP 消费 /reserve-analysis，按资料实际结果输出，
        # 不按资料名返回静态结论。
        for case in request(base, '/study-cases')['cases']:
            response = request(base, '/reserve-analysis', {'case': case})
            print(json.dumps({'reserve_analysis': case, 'response': response},
                             ensure_ascii=False))
        server.shutdown()
        worker.join(timeout=3)


if __name__ == '__main__':
    main()
