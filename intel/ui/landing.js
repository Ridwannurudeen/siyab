(() => {
  "use strict";
  const landing = document.querySelector(".landing");
  if (!landing) return;
  const sequences = Array.from(landing.querySelectorAll("[data-landing-sequence]"));
  const preference = matchMedia("(prefers-reduced-motion: reduce)");
  const counter = landing.querySelector(".landing-counter"), total = counter.textContent;
  const replay = landing.querySelector(".landing-replay");
  let frame, observer;
  const finish = sequence => {
    sequence.classList.remove("is-running");
    sequence.classList.add("is-complete");
    if (sequence === sequences[0]) replay.disabled = false;
  };
  const finishAll = () => {
    observer?.disconnect();
    cancelAnimationFrame(frame);
    counter.textContent = total;
    sequences.forEach(finish);
    landing.classList.remove("landing-motion-ready");
    replay.hidden = true;
  };
  if (typeof IntersectionObserver !== "function" || typeof landing.getAnimations !== "function") {
    finishAll(); return;
  }
  const run = sequence => {
    if (preference.matches || document.hidden) { finishAll(); return; }
    observer.unobserve(sequence);
    sequence.classList.remove("is-complete");
    sequence.classList.add("is-running");
    if (sequence === sequences[0]) {
      replay.disabled = true;
      counter.textContent = "0";
      const start = performance.now(), value = Number(total.replaceAll(",", ""));
      const tick = now => {
        const progress = Math.min((now - start) / 2200, 1);
        let low = 0, high = 1, t;
        for (let i = 0; i < 12; i++) {
          t = (low + high) / 2;
          if (3 * (1-t)**2 * t * .65 + 3 * (1-t) * t*t * .35 + t**3 < progress) low = t;
          else high = t;
        }
        counter.textContent = progress < 1 ? Math.round(value * t*t * (3 - 2*t)).toLocaleString("en-US") : total;
        if (progress < 1) frame = requestAnimationFrame(tick);
        else cancelAnimationFrame(frame);
      };
      frame = requestAnimationFrame(tick);
    }
    const animations = sequence.getAnimations({subtree: true});
    Promise.all(animations.map(animation => animation.finished)).then(() => finish(sequence), () => finish(sequence));
  };
  observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting && entry.intersectionRatio >= .35) {
      run(entry.target);
    }
  }, {threshold: .35});
  const update = () => {
    if (preference.matches || document.hidden) finishAll();
    else { landing.classList.add("landing-motion-ready"); replay.hidden = false; }
  };
  document.addEventListener("visibilitychange", update);
  preference.addEventListener("change", update);
  replay.addEventListener("click", () => run(sequences[0]));
  update();
  if (!preference.matches && !document.hidden) {
    counter.textContent = "0";
    sequences.forEach(sequence => observer.observe(sequence));
  }
})();
