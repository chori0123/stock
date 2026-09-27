// 최소한의 서비스워커: 홈 화면에 추가된 아이콘을 "설치된 웹앱"처럼 인식시키기 위한 용도.
// 오프라인 캐싱은 하지 않는다 (시세 데이터는 항상 최신을 봐야 하므로 매 요청 네트워크로 통과).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(fetch(event.request)));
