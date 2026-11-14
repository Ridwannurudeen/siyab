(() => {
  "use strict";
  const landing = document.querySelector(".landing");
  if (!landing) return;
  const sequences = Array.from(landing.querySelectorAll("[data-landing-sequence]"));
  const preference = matchMedia("(prefers-reduced-motion: reduce)");
  const finish = sequence => {
    sequence.classList.remove("is-running");
    sequence.classList.add("is-complete");
  };
  const finishAll = () => {
    sequences.forEach(finish);
    landing.classList.remove("landing-motion-ready");
  };
  if (preference.matches || document.hidden || typeof IntersectionObserver !== "function"
      || typeof landing.getAnimations !== "function") {
    finishAll(); return;
  }
  landing.classList.add("landing-motion-ready");
  for (const sequence of sequences) {
    sequence.classList.add("is-running");
    const animations = sequence.getAnimations({subtree: true});
    Promise.all(animations.map(animation => animation.finished)).then(() => finish(sequence), () => finish(sequence));
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) finishAll(); });
  preference.addEventListener("change", event => { if (event.matches) finishAll(); });
})();
