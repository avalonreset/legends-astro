/* Canary stand-in: ID lookups, body classes, hidden toggles, JS-created .rung-btn. */
const analyticsDecision = document.getElementById("analyticsDecision");
const emailGate = document.getElementById("emailGate");
const unlockBtn = document.getElementById("unlockBtn");
const identityDoors = document.getElementById("identityDoors");
const doorGithub = document.getElementById("doorGithub");
const doorGoogle = document.getElementById("doorGoogle");
const shrinePanel = document.getElementById("shrinePanel");
const rungsMonthEl = document.getElementById("rungsMonth");
const rungsYearEl = document.getElementById("rungsYear");
const rungsOnceEl = document.getElementById("rungsOnce");
const reviewModal = document.getElementById("reviewModal");
const reviewBack = document.getElementById("reviewBack");
const reviewContinue = document.getElementById("reviewContinue");
const voicePanel = document.getElementById("voicePanel");
void unlockBtn;
void identityDoors;
void doorGithub;
void doorGoogle;
void rungsYearEl;
void rungsOnceEl;

function addPathCell(listEl, label) {
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rung-btn";
  btn.textContent = label;
  btn.addEventListener("click", () => {
    document.querySelectorAll(".rung-btn.selected").forEach((node) => node.classList.remove("selected"));
    btn.classList.add("selected");
    if (reviewModal) reviewModal.hidden = false;
  });
  li.appendChild(btn);
  listEl.appendChild(li);
}

if (rungsMonthEl) {
  addPathCell(rungsMonthEl, "$1·mo");
}

if (analyticsDecision) {
  analyticsDecision.addEventListener("click", (event) => {
    const button = event.target.closest("[data-initial-analytics-choice]");
    if (!button) return;
    document.body.classList.add("hard-identity", "mode-shrine", "email-unlocked");
    document.documentElement.classList.add("hard-identity", "email-unlocked");
    if (emailGate) {
      emailGate.hidden = false;
      emailGate.removeAttribute("hidden");
    }
    if (shrinePanel) shrinePanel.hidden = false;
    if (voicePanel) {
      voicePanel.hidden = false;
      voicePanel.removeAttribute("hidden");
    }
  });
}

if (reviewBack) {
  reviewBack.addEventListener("click", () => {
    if (reviewModal) reviewModal.hidden = true;
  });
}

if (reviewContinue) {
  reviewContinue.addEventListener("click", (event) => event.preventDefault());
}
