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
  window.SiyabNumbers = Object.freeze({decimal, baseUnits});
})();
