// Google OAuth relay for tools/google-mcp/phone-connect.py (no server code, no restart).
// Google can only redirect a Web client to a public https address, so it lands here on
// Walter's public app URL. The OAuth "state" says where on the home network the connect
// script is listening ("<nonce>.<base64url(http://192.168.x.y:PORT/TOKEN/cb)>"); this page
// forwards the query (code + state) there and nowhere else: only http:// private IPv4
// addresses are accepted, so it can't be used as an open redirect to the internet.
// The code alone is useless: it needs the client secret and the PKCE verifier, which
// only the connect script holds.
(function () {
  var msg = document.getElementById("msg");
  var q = new URLSearchParams(location.search);
  var state = q.get("state") || "";
  var dot = state.indexOf(".");
  var back = "";
  try {
    var b = state.slice(dot + 1).replace(/-/g, "+").replace(/_/g, "/");
    while (b.length % 4) b += "=";
    back = dot > 0 ? atob(b) : "";
  } catch (e) { back = ""; }
  var ok = /^http:\/\/(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}):\d{2,5}\/[A-Za-z0-9_-]{8,64}\/cb$/.test(back);
  if (!ok) {
    msg.textContent = "This link didn't come from Walter's connect page, so there's nothing to hand back. " +
      "Ask Walter to show the Google connect code again.";
    return;
  }
  var target = back + "?" + q.toString();
  history.replaceState(null, "", location.pathname);   // don't keep the code in history
  msg.innerHTML = "";
  msg.appendChild(document.createTextNode("If nothing happens in a few seconds, "));
  var a = document.createElement("a"); a.href = target; a.textContent = "tap here";
  msg.appendChild(a); msg.appendChild(document.createTextNode(" (your phone needs to be on the home Wi-Fi)."));
  location.replace(target);
})();
