READING THESE SHEETS — what the schema cannot say.

Read by `pack_kit.learn` and placed in front of every sheet. Keep it short: it
is paid for on each one. The vocabulary itself lives in ontology/schema.ttl.

SHORTHAND USED BY THE OPERATORS
- Ts, Tsubs, Tgrowth, T_sample, "Sample T": temperature of the SAMPLE, °C.
- T next to a cell or a material name (TSe, "Bi T = 494°C", "Se (cell 1)
  170°C"): temperature of that CELL, °C. Several numbers with slashes
  ("335/500/1000°C") are the zones of one cell.
- P followed by %: a POWER. Of the substrate heater when it stands with the
  sample temperature ("set P = 35%", "Pcell = 47.2 %" under Substrate
  parameters); of an electron gun when it stands with If, Ie, HV; of a cell
  when it follows that cell's temperature ("Ge 1098°C 65.2%").
- P followed by mbar or a power of ten (P_Se, PSe, Pch, "1.5e-9/9.8e-8"): a
  PRESSURE. dP is the beam pressure of a cell (reading minus background). Pch is
  the chamber base pressure.
- E-gun, EBE, e-beam, canon: an electron gun that evaporates material. "W :
  E-gun 1, position 2" = gun number 1, charge at position 2, material W.
- If, Ifilament: filament current. Ie: emission current. HV, HT followed by kV:
  high voltage of an electron gun. (HT followed by a temperature is a heat
  treatment, an annealing.)
- xtc: quartz reading, an ARBITRARY unit proper to one gun and one material.
  Vxtc, vxtc: rate in xtc per second. "density", "d/40", "d/80": the density
  setting of the quartz controller, d being the material's true density. TF:
  tooling factor.
- ML, MLs, L: monolayers. 5' = 5 minutes, 20'' = 20 seconds, mn = minutes.
- In the RHEED lines, the "e-gun" that is on or off, Uf, Ua, UG1, H, V, theta
  and the kV belong to the RHEED gun — not to an evaporation source.
- "1-0607", "2-0356": sample numbers. In brackets or before a colon, an EARLIER
  sample the operator refers to.
- French and English are mixed freely: dépôt = deposition, recuit = annealing,
  dégazage = degassing, cache = shutter, cellule = cell, canon = e-gun.

NEVER
- Never compute, convert or correct. "442/2 = 167" is reported as written;
  "21 mn 5 s" stays "21 mn 5 s"; a number keeps the unit the sheet gave it and
  gets none the sheet did not give.
- Never merge two sources into one sentence without saying which value is
  whose. A co-evaporation is several sources during ONE step.
- Never turn an incident into a step, nor one operator's choice into a rule.
- Never drop a remark because it fits nowhere: keep it, verbatim, with the
  step it is attached to.
- Never alter a marker "[IMAGE: ...]", a file name, a sample number, a
  chemical formula or a control command. Copy them character for character.

A header line "not filled in" and a "Caution:" line come from the reader of the
file, not from the operator: respect the first, repeat the second.
