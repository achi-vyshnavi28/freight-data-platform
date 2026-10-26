# Freight operations findings

_Generated 2026-10-26 14:56 by `python -m fdp.pipeline report` from the dbt marts._

## Headline
- **1,193 delivered shipments**, **75.7% on time**, ₹53,731,170 freight billed.
- **₹360,070 overbilled** across 93 invoices (freight above the contract rate in force at dispatch, beyond a 2% tolerance).
- **Godavari Freight Carriers** accounts for the most overbilling: 30.0% of its invoices, ₹299,220.
- **Mumbai -> Hyderabad** is the least reliable lane: 18.4% on time, p90 transit 32.4 h.

## Transporter scorecard
| transporter | shipments | on time pct | overbilled pct | overbilled amount | invoices with errors | delivered not invoiced |
|---|---|---|---|---|---|---|
| Godavari Freight Carriers | 268 | 73.5 | 30.0 | 299,220 | 2 | 5 |
| Deccan Cargo Movers | 238 | 84.0 | 1.7 | 18,310 | 2 | 4 |
| Sri Balaji Roadways | 238 | 77.3 | 2.2 | 17,060 | 2 | 8 |
| Western Ghats Logistics | 224 | 79.0 | 1.4 | 16,610 | 1 | 9 |
| Krishna Transport Co | 225 | 64.4 | 0.9 | 8,870 | 5 | 4 |

## Lane reliability
| lane | shipments | on time pct | p90 transit hours |
|---|---|---|---|
| Mumbai -> Hyderabad | 109 | 18.4 | 32.4 |
| Hyderabad -> Mumbai | 129 | 22.5 | 32.1 |
| Hyderabad -> Bangalore | 135 | 85.9 | 20.6 |
| Hyderabad -> Nagpur | 124 | 87.1 | 17.5 |
| Chennai -> Hyderabad | 118 | 87.3 | 21.4 |
| Hyderabad -> Pune | 106 | 89.6 | 19.8 |
| Hyderabad -> Chennai | 115 | 90.4 | 19.7 |
| Hyderabad -> Vijayawada | 132 | 90.9 | 11.7 |
| Hyderabad -> Visakhapatnam | 112 | 91.1 | 19.8 |
| Bangalore -> Hyderabad | 113 | 93.8 | 18.9 |

## What drives lateness
| trip type | shipments | on time pct | avg delay hours when late |
|---|---|---|---|
| had a stop over 60 min | 478 | 66.7 | 4.4 |
| no long stop | 715 | 81.7 | 3.5 |

## Revenue leakage between TMS and ERP
| issue | cases | amount |
|---|---|---|
| delivered, never invoiced | 30 | 1,388,900 |
| invoice for unknown shipment | 7 | 415,184 |

## Why the rate join must be effective-dated
Rates rose 6% on 1 July 2026. Comparing invoices with the **latest** rate card flags 74 invoices;
comparing with the rate **in force on the dispatch date** flags 93. The naive query silently misses
**19** overbilled invoices, because padded June bills look normal against the higher July rate.

## Data quality this run
| check | value |
|---|---|
| shipments quarantined | 7 |
| gps pings quarantined | 1933 |
| invoices failing arithmetic | 12 |
| invoices with revisions | 0 |
