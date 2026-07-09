// k6 压测(闭环④ NFR 传感器)—— 验证 500 并发受理 P95 ≤ 10s(NFR-1)
// 跑法:先起服务  .venv/bin/uvicorn app.main:app --port 8099
//       再  BASE=http://localhost:8099 k6 run scripts/load_test.js
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    concurrent_users: { executor: 'constant-vus', vus: 500, duration: '30s' },
  },
  thresholds: {
    http_req_duration: ['p(95)<10000'],   // NFR-1:受理 P95 ≤ 10s
    http_req_failed: ['rate<0.01'],
  },
};

const BASE = __ENV.BASE || 'http://localhost:8099';

export default function () {
  // 视频上传受理(核心路径 REQ-201)
  const res = http.post(
    `${BASE}/videos`,
    JSON.stringify({ oss_key: `load-${__VU}.mp4`, size_bytes: 1000 }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, { 'accepted 200': (r) => r.status === 200 });
}
