// スライムこうぼう ─ ホーム画面アプリにするための係。
//
// やることは2つだけ：
//   1) 絵と本体を手元に持っておく（次から一瞬で開く。電波が悪くても絵は出る）
//   2) PCの様子を聞く通信（/api/…）には一切手を出さない（常に最新を見に行く）

const CACHE = "slime-koubou-v1";

// 最初に手元へ取り込んでおくもの
const SHELL = [
  "/",
  "/index.html",
  "/manifest.json",
  "/lib/kaplay.js",
  "/room.png",
  "/sheet_green.png",
  "/sheet_pink.png",
  "/sheet_blue.png",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // 1つでも失敗したら全部やめる…では困るので、取れたものだけ入れる
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // PCの様子を聞く通信は、絶対に手元の古い答えを返さない
  if (url.pathname.startsWith("/api/")) return;

  // 本体（index.html）は「まず最新を取りに行く／だめなら手元」。
  // こうしないと、PC側で画面を直したのにスマホが古いまま、が起きる
  if (req.mode === "navigate" || url.pathname === "/" || url.pathname === "/index.html") {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/index.html", copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/index.html"))
    );
    return;
  }

  // 絵などは「まず手元／なければ取りに行く」
  e.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    }))
  );
});
