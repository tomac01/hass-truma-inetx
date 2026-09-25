#!/usr/bin/env node
/**
 * Prüft die Wrapper-Karte für die Vorgangsrückmeldung.
 *
 * Warum das hier steht: Die Karte umschließt eine beliebige andere Karte.
 * Würde sie bei jedem State-Update deren DOM neu bauen, verlöre jede
 * Eingabe den Fokus -- und ein Fehlertext vom Gerät, der als HTML
 * eingesetzt wird, wäre eine Einladung.
 *
 * Was der Test festnagelt:
 *   1. die Karte wird NICHT auf Modulebene definiert, sondern erst, wenn
 *      "home-assistant" da ist -- und zwar im selben, einzigen whenDefined-
 *      Block wie die Dial-Karte, die dabei erhalten bleibt,
 *   2. nur passende Vorgänge lösen den Busy-Zustand aus,
 *   3. Fehlertexte landen als Text, nie als Markup,
 *   4. ohne Kindkarte wird nichts animiert,
 *   5. die Kindkarte wird genau einmal gebaut und danach nur noch mit `hass`
 *      versorgt -- kein DOM-Neubau je State-Update,
 *   6. reduced motion und role="status" stehen im Shadow-Template dieser
 *      Karte (nicht irgendwo sonst in der Datei).
 *
 * Der Stub ist mit Absicht streng: `innerHTML` auf den Innenelementen wirft,
 * `window.customCards` existiert anfangs nicht, und `whenDefined` liefert ein
 * Promise, das der Test selbst auflöst. Nur so lässt sich überhaupt
 * beobachten, ob vor dem Auflösen schon etwas definiert war.
 *
 * Run: node tests/test_operation_card.cjs
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(
  __dirname, "..", "custom_components", "truma_inetx", "frontend",
  "truma-climate-dial-card.js"
);
const code = fs.readFileSync(SOURCE, "utf8");

const ENTITY = "sensor.vorgang";

/** Leert die Mikrotask-Queue (und damit alle .then-Ketten der Karte). */
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Minimal-Element, das jeden innerHTML-Zugriff als Fehler meldet. */
function element(name) {
  const el = {
    name,
    appended: [],
    attributes: {},
    textContent: "",
    hidden: false,
    classList: {
      _set: new Set(),
      toggle(cls, on) { on ? this._set.add(cls) : this._set.delete(cls); },
      contains(cls) { return this._set.has(cls); },
    },
    setAttribute(key, value) { this.attributes[key] = value; },
    getAttribute(key) { return this.attributes[key]; },
    append(...nodes) { this.appended.push(...nodes); },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return undefined; },
    set(value) {
      throw new Error(`innerHTML auf <${name}> gesetzt: ${String(value)}`);
    },
  });
  return el;
}

// --- Sandbox ---------------------------------------------------------------

const defined = new Map();
const whenDefinedArgs = [];
const whenDefinedResolvers = [];

// Standard-Helfer: liefert für jede Konfiguration ein frisches Kind und zählt
// mit, wie oft gebaut wurde.
const helpers = { created: [], impl: null };
function defaultHelpers() {
  return Promise.resolve({
    createCardElement(config) {
      const child = element("child");
      child.config = config;
      child.getCardSize = () => 3;
      helpers.created.push(child);
      return child;
    },
  });
}
helpers.impl = defaultHelpers;

const context = {
  console,
  customElements: {
    get: (name) => defined.get(name),
    define: (name, cls) => defined.set(name, cls),
    whenDefined: (name) => {
      whenDefinedArgs.push(name);
      return new Promise((resolve) => whenDefinedResolvers.push(resolve));
    },
  },
  // Absichtlich OHNE customCards: wer den Eintrag vor der `|| []`-Zeile
  // pusht, fliegt hier auf die Nase -- im Browser genauso.
  window: { loadCardHelpers: () => helpers.impl() },
};
context.window.customElements = context.customElements;
context.globalThis = context;
context.HTMLElement = class {
  constructor() { this._shadow = null; }
  attachShadow() {
    const shadow = {
      html: "",
      control: element("control"),
      status: element("status"),
      get innerHTML() { return this.html; },
      set innerHTML(value) {
        // Wie im echten DOM: neues Markup wirft die alten Kinder weg.
        this.html = value;
        this.control = element("control");
        this.status = element("status");
      },
      querySelector(selector) {
        if (selector === ".control") return this.control;
        if (selector === ".status") return this.status;
        throw new Error(`unerwarteter Selektor: ${selector}`);
      },
    };
    this._shadow = shadow;
    return shadow;
  }
  get shadowRoot() { return this._shadow; }
};
vm.createContext(context);
vm.runInContext(code, context);

// --- 1. Registrierung ------------------------------------------------------

// Vor dem Auflösen von whenDefined darf NICHTS definiert sein. Genau hier
// fällt eine Definition auf Modulebene auf -- der Fehler, den der
// whenDefined-Block behebt.
assert.equal(
  defined.get("truma-operation-card"), undefined,
  "truma-operation-card wurde auf Modulebene definiert -- sie gehört in den "
  + "bestehenden whenDefined(\"home-assistant\")-Block"
);
assert.equal(
  defined.get("truma-climate-dial-card"), undefined,
  "die Dial-Karte wurde auf Modulebene definiert"
);
assert.deepEqual(
  whenDefinedArgs, ["home-assistant"],
  "es darf genau einen whenDefined(\"home-assistant\")-Block geben, und beide "
  + "Karten gehören hinein -- gefunden: " + JSON.stringify(whenDefinedArgs)
);

async function main() {
  whenDefinedResolvers.forEach((resolve) => resolve());
  await tick();

  const Card = defined.get("truma-operation-card");
  assert.ok(Card, "truma-operation-card wurde nicht registriert");
  assert.ok(
    defined.get("truma-climate-dial-card"),
    "die Dial-Karte ist bei der Erweiterung verlorengegangen"
  );

  // Kartenwähler: beide Karten, und customCards wurde sauber angelegt.
  assert.ok(Array.isArray(context.window.customCards), "customCards fehlt");
  const entry = context.window.customCards.find(
    (item) => item.type === "truma-operation-card"
  );
  assert.ok(entry, "die Karte fehlt im Kartenwähler");
  assert.ok(entry.name && entry.description, "Name/Beschreibung fehlen");
  assert.ok(
    context.window.customCards.some(
      (item) => item.type === "truma-climate-dial-card"
    ),
    "die Dial-Karte fehlt im Kartenwähler"
  );

  // --- 2. setConfig prüft die Entität --------------------------------------

  assert.throws(
    () => new Card().setConfig({}), /operation_entity/,
    "eine Karte ohne operation_entity muss sich weigern"
  );
  assert.throws(
    () => new Card().setConfig({ operation_entity: "climate.truma" }),
    /operation_entity/,
    "nur ein Sensor taugt als Vorgangsentität"
  );

  // --- 3. Das Shadow-Template ----------------------------------------------

  const bare = new Card();
  bare.setConfig({ operation_entity: ENTITY });
  const html = bare.shadowRoot.innerHTML;
  assert.match(html, /role="status"/, "role=\"status\" fehlt");
  assert.match(html, /aria-live="polite"/, "aria-live fehlt");
  assert.match(
    html, /prefers-reduced-motion:\s*reduce\)[\s\S]{0,160}animation:\s*none/,
    "reduced motion schaltet die Animation nicht ab"
  );

  // --- 4. Die Rückmeldung selbst -------------------------------------------

  function feedbackFor(config, state, language = "de") {
    const card = new Card();
    card.setConfig(config);
    card.hass = {
      language,
      states: state ? { [ENTITY]: state } : {},
    };
    return card._feedback();
  }

  const config = { operation_entity: ENTITY, action: "energy_source" };

  // Nur passende Vorgänge machen busy.
  const mine = feedbackFor(config, {
    state: "changing", attributes: { action: "energy_source" },
  });
  assert.equal(mine.busy, true);
  assert.equal(mine.error, false);
  assert.equal(mine.text, "Wird umgestellt …");
  assert.equal(
    feedbackFor(config, {
      state: "changing", attributes: { action: "temperature" },
    }).busy,
    false,
    "ein fremder Vorgang hat die Karte pulsieren lassen"
  );
  assert.equal(
    feedbackFor(config, {
      state: "changing", attributes: { action: "temperature" },
    }).text,
    "",
    "ein fremder Vorgang darf auch keinen Text erzeugen"
  );

  // Sync ist ein eigener Text.
  assert.equal(
    feedbackFor(config, {
      state: "syncing", attributes: { action: "energy_source" },
    }).text,
    "Synchronisation läuft …"
  );
  assert.equal(
    feedbackFor(config, {
      state: "syncing", attributes: { action: "energy_source" },
    }, "en").text,
    "Synchronizing …"
  );

  // Array-Filter, beide Richtungen.
  const many = {
    operation_entity: ENTITY, action: ["hvac_mode", "temperature"],
  };
  assert.equal(
    feedbackFor(many, {
      state: "syncing", attributes: { action: "temperature" },
    }).busy,
    true
  );
  assert.equal(
    feedbackFor(many, {
      state: "syncing", attributes: { action: "water_mode" },
    }).busy,
    false,
    "der Array-Filter lässt fremde Vorgänge durch"
  );

  // Ohne Filter zählt jeder Vorgang, und im Leerlauf steht "Bereit".
  const nofilter = { operation_entity: ENTITY };
  assert.equal(
    feedbackFor(nofilter, {
      state: "changing", attributes: { action: "was_auch_immer" },
    }).busy,
    true
  );
  assert.equal(
    feedbackFor(nofilter, { state: "idle", attributes: {} }).text, "Bereit"
  );
  assert.equal(
    feedbackFor(nofilter, { state: "idle", attributes: {} }, "en").text, "Ready"
  );
  assert.equal(
    feedbackFor(config, { state: "idle", attributes: {} }).text, "",
    "eine Karte mit Action-Filter soll im Leerlauf schweigen"
  );

  // Unbekannter Sensor -- fehlend, "unknown" und "unavailable" gleich.
  for (const state of [
    null,
    { state: "unknown", attributes: {} },
    { state: "unavailable", attributes: {} },
  ]) {
    const feedback = feedbackFor(config, state);
    assert.equal(
      feedback.text, "Vorgangsstatus nicht verfügbar",
      `Zustand ${JSON.stringify(state)} gilt nicht als unbekannt`
    );
    assert.equal(feedback.busy, false);
    assert.equal(feedback.error, false);
  }
  assert.equal(
    feedbackFor(config, null, "en").text, "Operation status unavailable"
  );

  // Sprache: Regionalvarianten zählen mit.
  assert.equal(
    feedbackFor(config, null, "de-DE").text, "Vorgangsstatus nicht verfügbar"
  );
  assert.equal(
    feedbackFor(config, null, "en-GB").text, "Operation status unavailable"
  );

  // Fehler: passend, unpassend, ohne Fehlertext.
  const failed = feedbackFor(config, {
    state: "error", attributes: { action: "energy_source", error: "Timeout" },
  });
  assert.equal(failed.error, true);
  assert.equal(failed.busy, false);
  assert.equal(failed.text, "Fehler: Timeout");
  assert.equal(
    feedbackFor(config, {
      state: "error", attributes: { action: "temperature", error: "Timeout" },
    }).error,
    false,
    "ein fremder Fehler wurde als eigener gemeldet"
  );
  assert.equal(
    feedbackFor(config, {
      state: "error", attributes: { action: "energy_source" },
    }).text,
    "Fehler: Vorgang fehlgeschlagen"
  );
  assert.equal(
    feedbackFor(config, {
      state: "error", attributes: { action: "energy_source" },
    }, "en").text,
    "Error: Operation failed"
  );

  // --- 5. Fehlertext bleibt Text -------------------------------------------

  const attack = '<img src=x onerror=alert(1)>';
  const xss = new Card();
  xss.setConfig({ operation_entity: ENTITY, card: { type: "tile" } });
  xss.hass = {
    language: "de",
    states: {
      [ENTITY]: {
        state: "error",
        attributes: { action: "water_mode", error: attack },
      },
    },
  };
  await tick();
  assert.equal(
    xss.shadowRoot.status.textContent, `Fehler: ${attack}`,
    "Fehlertext muss Text bleiben, nicht Markup werden"
  );
  assert.equal(xss.shadowRoot.status.classList.contains("error"), true);
  assert.equal(xss.shadowRoot.status.hidden, false);
  assert.equal(
    xss.shadowRoot.control.classList.contains("busy"), false,
    "ein Fehler ist kein Busy-Zustand"
  );

  // --- 6. Animation nur mit Kindkarte --------------------------------------

  const busyState = {
    language: "de",
    states: {
      [ENTITY]: { state: "changing", attributes: { action: "x" } },
    },
  };

  const statusOnly = new Card();
  statusOnly.setConfig({ operation_entity: ENTITY });
  statusOnly.hass = busyState;
  await tick();
  assert.equal(
    statusOnly.shadowRoot.control.classList.contains("busy"), false,
    "eine reine Statusanzeige darf keinen leeren Rahmen animieren"
  );

  const wrapped = new Card();
  wrapped.setConfig({ operation_entity: ENTITY, card: { type: "tile" } });
  wrapped.hass = busyState;
  await tick();
  assert.equal(
    wrapped.shadowRoot.control.classList.contains("busy"), true,
    "mit Kindkarte muss der Busy-Zustand sichtbar werden"
  );
  assert.equal(
    wrapped.shadowRoot.control.getAttribute("aria-busy"), "true",
    "aria-busy fehlt"
  );
  assert.equal(wrapped.shadowRoot.status.textContent, "Wird umgestellt …");
  assert.equal(wrapped.shadowRoot.status.hidden, false);

  // Und wieder zurück in den Leerlauf: Klasse weg. Diese Karte hat keinen
  // Action-Filter, also bleibt "Bereit" stehen.
  wrapped.hass = {
    language: "de",
    states: { [ENTITY]: { state: "idle", attributes: {} } },
  };
  await tick();
  assert.equal(wrapped.shadowRoot.control.classList.contains("busy"), false);
  assert.equal(wrapped.shadowRoot.control.getAttribute("aria-busy"), "false");
  assert.equal(wrapped.shadowRoot.status.textContent, "Bereit");
  assert.equal(wrapped.shadowRoot.status.hidden, false);

  // Mit Action-Filter dagegen schweigt die Karte im Leerlauf -- und eine leere
  // Statuszeile muss verschwinden, sonst bleibt ein Polster im Layout stehen.
  const filtered = new Card();
  filtered.setConfig({ operation_entity: ENTITY, action: "energy_source" });
  filtered.hass = {
    language: "de",
    states: { [ENTITY]: { state: "idle", attributes: {} } },
  };
  await tick();
  assert.equal(filtered.shadowRoot.status.textContent, "");
  assert.equal(
    filtered.shadowRoot.status.hidden, true,
    "eine leere Statuszeile muss verschwinden"
  );

  // --- 7. Die Kindkarte wird genau einmal gebaut ---------------------------

  helpers.created.length = 0;
  const stable = new Card();
  stable.setConfig({ operation_entity: ENTITY, card: { type: "tile" } });
  // hass kommt VOR der Kindkarte an -- sie muss es trotzdem bekommen.
  stable.hass = busyState;
  await tick();
  assert.equal(
    helpers.created.length, 1,
    "die Kindkarte muss genau einmal gebaut werden -- gebaut: "
    + helpers.created.length
  );
  const child = helpers.created[0];
  assert.deepEqual(child.config, { type: "tile" });
  assert.equal(child.hass, busyState, "die Kindkarte kennt hass nicht");
  assert.deepEqual(
    stable.shadowRoot.control.appended, [child],
    "die Kindkarte hängt nicht im Rahmen"
  );

  for (let i = 0; i < 3; i += 1) {
    stable.hass = {
      language: "de",
      states: {
        [ENTITY]: { state: "changing", attributes: { action: `x${i}` } },
      },
    };
    await tick();
  }
  assert.equal(
    helpers.created.length, 1,
    "die Kindkarte wurde bei einem State-Update neu gebaut -- dabei geht "
    + "jede Eingabe und jeder Fokus verloren"
  );
  assert.deepEqual(
    stable.shadowRoot.control.appended, [child],
    "die Kindkarte wurde erneut eingehängt"
  );
  assert.equal(
    child.hass.states[ENTITY].attributes.action, "x2",
    "die Kindkarte bekommt keine neuen States mehr"
  );
  assert.equal(await stable.getCardSize(), 4, "getCardSize zählt falsch");
  assert.equal(await bare.getCardSize(), 2, "getCardSize ohne Kind");

  // --- 8. setConfig zweimal: die alte Kindkarte darf nicht nachrücken ------

  helpers.created.length = 0;
  const pending = [];
  helpers.impl = () => new Promise((resolve) => pending.push(resolve));

  const reconfigured = new Card();
  reconfigured.setConfig({ operation_entity: ENTITY, card: { type: "alt" } });
  reconfigured.setConfig({ operation_entity: ENTITY, card: { type: "neu" } });
  const factory = {
    createCardElement(cardConfig) {
      const el = element("child");
      el.config = cardConfig;
      helpers.created.push(el);
      return el;
    },
  };
  pending.forEach((resolve) => resolve(factory));
  await tick();
  assert.equal(
    reconfigured.shadowRoot.control.appended.length, 1,
    "eine überholte setConfig hat ihre Kindkarte nachträglich eingehängt"
  );
  assert.deepEqual(
    reconfigured.shadowRoot.control.appended[0].config, { type: "neu" },
    "die falsche Kindkarte hat gewonnen"
  );
  helpers.impl = defaultHelpers;

  // --- 9. Scheitert der Helfer, steht das im Status ------------------------

  helpers.impl = () => Promise.reject(new Error("kaputte Kartenkonfiguration"));
  const broken = new Card();
  broken.setConfig({ operation_entity: ENTITY, card: { type: "gibtsnicht" } });
  broken.hass = busyState;
  await tick();
  assert.equal(
    broken.shadowRoot.status.textContent, "kaputte Kartenkonfiguration",
    "der Baufehler steht nicht im Status"
  );
  assert.equal(broken.shadowRoot.status.classList.contains("error"), true);
  assert.equal(
    broken.shadowRoot.control.classList.contains("busy"), false,
    "ein leerer Rahmen darf auch bei laufendem Vorgang nicht pulsieren"
  );
  helpers.impl = defaultHelpers;

  console.log("ok  operation card");
  console.log("Operation card: all checks OK");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
