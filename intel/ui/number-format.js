(() => {
  "use strict";
  function decimal(value, places = 2) {
    if (!["string", "number"].includes(typeof value) || String(value).length > 160 || !Number.isInteger(places) || places < 0 || places > 24) return "Unavailable";
    const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value));
    if (!match) return "Unavailable";
    const exponent = Number(match[4] || 0);
    if (!Number.isInteger(exponent) || Math.abs(exponent) > 255) return "Unavailable";
    const fraction = match[3] || "", scale = fraction.length - exponent;
    let units = BigInt(match[2] + fraction), rounded;
    if (scale > places) {
      const divisor = 10n ** BigInt(scale - places);
      rounded = (units + divisor / 2n) / divisor;
    } else rounded = units * 10n ** BigInt(places - scale);
    if (units !== 0n && rounded === 0n) return (match[1] === "-" ? ">-" : "<") + (places ? "0." + "0".repeat(places - 1) + "1" : "1");
    const digits = rounded.toString().padStart(places + 1, "0");
    const whole = (places ? digits.slice(0, -places) : digits).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const tail = places ? digits.slice(-places).replace(/0+$/, "") : "";
    return (match[1] === "-" && rounded !== 0n ? "-" : "") + whole + (tail ? "." + tail : "");
  }
  function baseUnits(value, decimals, places = 4) {
    if (typeof value !== "string" || !/^\d{1,39}$/.test(value) || !Number.isInteger(decimals) || decimals < 0 || decimals > 255) return "Unavailable";
    return decimal(value + "e-" + decimals, places);
  }
  const duration = ms => {
    if (ms < 1000) return ms + " ms";
    const seconds = Math.floor(ms / 1000);
    if (seconds < 60) return seconds + " s";
    if (seconds < 3600) { const rest = seconds % 60; return Math.floor(seconds / 60) + " min" + (rest ? " " + rest + " s" : ""); }
    const minutes = Math.floor(seconds % 3600 / 60); return Math.floor(seconds / 3600) + " h" + (minutes ? " " + minutes + " min" : "");
  };
  const prose = value => value.replace(/(^|[^.,\w])([0-9]{1,39}) yoctoNEAR\b/g, (whole, lead, amount) => { const near = baseUnits(amount, 24); return near === "Unavailable" ? whole : lead + near + " NEAR"; })
    .replace(/(^|[^.,\w])([0-9]{1,15}) ms\b/g, (whole, lead, ms) => lead + duration(Number(ms)));
  window.SiyabNumbers = Object.freeze({decimal, baseUnits, duration, prose});
})();
