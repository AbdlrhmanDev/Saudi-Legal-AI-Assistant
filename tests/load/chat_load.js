// k6 load test for the public chat path (PRD v2 NFR: chat p95 <= 8 s).
//
//   k6 run -e BASE_URL=https://staging-api.example.com tests/load/chat_load.js
//
// Ramps to 150 concurrent users (a busy hour for ~1,000-3,000 daily users) with think time.
// Each virtual user presents its own X-Forwarded-For address so per-client rate limits behave as
// they would in production (the API must run with TRUSTED_PROXY_COUNT matching your proxies).
import http from 'k6/http'
import { check, sleep } from 'k6'

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000'
const QUESTIONS = [
  'كم مدة فترة التجربة في نظام العمل؟',
  'ما مدة الإجازة السنوية للعامل في نظام العمل؟',
  'ما الحد الأدنى لرأس مال الشركة ذات المسؤولية المحدودة؟',
  'ما حق صاحب البيانات في الوصول إلى بياناته؟',
  'متى يجب إيداع أجر العامل؟',
  'ما عقوبة تهريب الآثار؟', // out of corpus: must abstain
]

export const options = {
  stages: [
    { duration: '2m', target: 50 },
    { duration: '5m', target: 150 },
    { duration: '2m', target: 0 },
  ],
  thresholds: {
    'http_req_duration{endpoint:chat}': ['p(95)<8000'],
    'http_req_failed{endpoint:chat}': ['rate<0.01'],
    'http_req_duration{endpoint:freshness}': ['p(95)<500'],
  },
}

export default function () {
  const headers = { 'Content-Type': 'application/json', 'X-Forwarded-For': `198.18.${__VU % 250}.${(__VU >> 8) + 1}` }
  const query = QUESTIONS[Math.floor(Math.random() * QUESTIONS.length)]
  const res = http.post(`${BASE_URL}/api/v1/chat`, JSON.stringify({ query }), { headers, tags: { endpoint: 'chat' } })
  check(res, {
    'chat 200': (r) => r.status === 200,
    'has freshness': (r) => r.status !== 200 || r.json('data.source_health') !== undefined,
  })
  if (Math.random() < 0.2) {
    http.get(`${BASE_URL}/api/v1/freshness`, { tags: { endpoint: 'freshness' } })
  }
  sleep(5 + Math.random() * 10) // reading the answer before the next question
}
