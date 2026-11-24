// 매출 대시보드 사이트 ↔ 확장프로그램 연결
// 사이트가 {kdApp:'ping'} 을 보내면 확장프로그램이 있다고 알려주고, {kdApp:'run'} 이면 정산·광고를 바로 받음
// (사이트 입구가 화면을 document.write 로 바꾸면서 리스너가 지워질 수 있어서, 주기적으로 다시 붙이고 먼저 알려줌)
const reply = m => window.postMessage(Object.assign({ kdExt: 1 }, m), location.origin);
function onMsg(e) {
  if (e.source !== window || !e.data || !e.data.kdApp) return;
  if (e.data.kdApp === 'ping') reply({ type: 'pong', v: chrome.runtime.getManifest().version });
  if (e.data.kdApp === 'run') {
    chrome.runtime.sendMessage({ cmd: 'runAll' }, r => {
      if (chrome.runtime.lastError) r = { ok: false, msg: '확장프로그램과 연결이 끊겼어요. 다시 눌러 주세요.' };
      reply(Object.assign({ type: 'result' }, r || { ok: false, msg: '응답 없음' }));
    });
  }
}
let n = 0;
function attach() {
  window.addEventListener('message', onMsg);   // 같은 함수는 한 번만 붙음
  if (n++ < 40) reply({ type: 'pong', v: chrome.runtime.getManifest().version });
}
attach();
setInterval(attach, 500);
