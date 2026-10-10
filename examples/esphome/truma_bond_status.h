#pragma once

// truma_bond_status.h
// Liest den BLE-Bond-Zustand des ESP32 aus dem Bluedroid-Sicherheitsspeicher.
// Eingebunden ueber "esphome: includes:" in truma-inetx-proxy.yaml.
// Enthaelt bewusst KEINE Geraeteadressen - alles entsteht zur Laufzeit.

#include "sdkconfig.h"

#if !defined(CONFIG_BT_BLUEDROID_ENABLED)
#error "truma_bond_status.h setzt den Bluedroid-Stack voraus (CONFIG_BT_BLUEDROID_ENABLED fehlt)."
#endif

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "esp_bt_defs.h"
#include "esp_err.h"
#include "esp_gap_ble_api.h"

// Hoechstzahl der Adressen, die in die Liste geschrieben werden.
// Laengster Fall: 8 Adressen zu 17 Zeichen plus 7 Kommas = 143 Zeichen, dazu
// hoechstens ",+99" = 4 Zeichen, also 147. Damit bleibt der Zustand unter der
// 255-Zeichen-Grenze eines Home-Assistant-Zustands.
static const int TRUMA_BOND_LIST_MAX = 8;

struct TrumaBondSnapshot {
  int count;         // >= 0: Zahl der Bonds im NVS; < 0: Abfrage fehlgeschlagen
  std::string list;  // leer, wenn count <= 0 oder die Liste nicht lesbar war
};

inline TrumaBondSnapshot truma_ble_bond_snapshot() {
  TrumaBondSnapshot snap;
  snap.count = -1;
  snap.list = "";

  // 1. Anzahl holen. Rueckgabe >= 0 ist die Anzahl, ESP_FAIL (-1) heisst
  //    "Bluedroid nicht bereit". Dann bleibt count auf -1 und der Aufrufer
  //    meldet nichts, statt eine Null zu veroeffentlichen, die wie "Bond weg"
  //    aussieht.
  const int num = esp_ble_get_bond_device_num();
  if (num < 0) {
    return snap;
  }
  snap.count = num;
  if (num == 0) {
    return snap;
  }

  // 2. Puffer. Die Groesse kommt aus der Anzahl, gedeckelt auf
  //    TRUMA_BOND_LIST_MAX. Der vector gibt den Speicher bei jedem Ruecksprung
  //    selbst wieder frei - es gibt keinen Pfad, auf dem er stehen bleibt.
  int capacity = num;
  if (capacity > TRUMA_BOND_LIST_MAX) {
    capacity = TRUMA_BOND_LIST_MAX;
  }
  std::vector<esp_ble_bond_dev_t> dev_list(static_cast<size_t>(capacity));

  // 3. dev_num ist Ein- UND Ausgabe: hinein die Kapazitaet des Puffers, heraus
  //    die Zahl der gefuellten Eintraege. Zur Vorsicht wird der
  //    Rueckgabewert unten gegen die Kapazitaet geklemmt, falls ein IDF-Stand
  //    die Gesamtzahl statt der gefuellten Eintraege zurueckschreibt.
  int written = capacity;
  const esp_err_t err = esp_ble_get_bond_device_list(&written, dev_list.data());
  if (err != ESP_OK) {
    return snap;  // Anzahl steht, Liste bleibt leer
  }
  if (written < 0) {
    written = 0;
  }
  if (written > capacity) {
    written = capacity;
  }

  // 4. Adressen formatieren. Nichts davon wird gespeichert.
  std::string out;
  char buf[18];  // "AA:BB:CC:DD:EE:FF" plus Nullbyte
  for (int i = 0; i < written; i++) {
    const uint8_t *a = dev_list[static_cast<size_t>(i)].bd_addr;
    snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X",
             a[0], a[1], a[2], a[3], a[4], a[5]);
    if (!out.empty()) {
      out += ",";
    }
    out += buf;
  }

  // 5. Wurden mehr Bonds gemeldet als in den Puffer passten, wird der Rest als
  //    "+n" angehaengt statt stillschweigend verschluckt.
  if (num > written) {
    out += ",+";
    out += std::to_string(num - written);
  }

  snap.list = out;
  return snap;
}

// Fordert das Loeschen ALLER Bonds aus dem Bluedroid-Speicher (NVS) an und
// liefert die Zahl der angenommenen Loeschauftraege, oder einen negativen Wert,
// wenn die Liste nicht lesbar war. esp_ble_remove_bond_device() arbeitet
// asynchron: ESP_OK heisst "Auftrag angenommen", nicht "geloescht".
// Nur fuer gezielte Neukopplungen gedacht: Danach kann
// der Proxy das Panel erst wieder erreichen, wenn neu gekoppelt wurde.
// Anders als "Factory reset" bleibt der Rest der NVS-Partition (WLAN usw.)
// unangetastet.
inline int truma_ble_remove_all_bonds() {
  const int num = esp_ble_get_bond_device_num();
  if (num <= 0) {
    return num;
  }
  std::vector<esp_ble_bond_dev_t> dev_list(static_cast<size_t>(num));
  int written = num;
  if (esp_ble_get_bond_device_list(&written, dev_list.data()) != ESP_OK) {
    return -1;
  }
  if (written > num) {
    written = num;
  }
  int removed = 0;
  for (int i = 0; i < written; i++) {
    if (esp_ble_remove_bond_device(dev_list[static_cast<size_t>(i)].bd_addr) == ESP_OK) {
      removed++;
    }
  }
  return removed;
}
