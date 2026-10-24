/* Canary stand-in of the production frame-guard contract. Not a copy of product JS. */
(function () {
  var ALLOW = /^(provCheckout)$/i;
  function isAllowed(el) {
    if (!el || !el.closest) return false;
    try {
      if (el.closest("#provCheckout, .prov-modal, [data-stripe-checkout]")) return true;
      var p = el.parentElement;
      while (p) {
        if (p.id && ALLOW.test(p.id)) return true;
        p = p.parentElement;
      }
    } catch (_) {}
    return false;
  }
  function kill(el) {
    if (!el || el.nodeType !== 1) return;
    var tag = (el.tagName || "").toUpperCase();
    if (tag !== "IFRAME" && tag !== "FRAME") return;
    if (isAllowed(el)) return;
    try { el.remove(); } catch (_) {}
  }
  function sweep(root) {
    try {
      if (!root || !root.querySelectorAll) return;
      var list = root.querySelectorAll("iframe,frame");
      for (var i = list.length - 1; i >= 0; i--) kill(list[i]);
    } catch (_) {}
  }
  sweep(document);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { sweep(document); });
  }
})();
