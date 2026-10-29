## Overview

This project implements the **Outage Planning Incompatibility (OPI)** assessment on **CGMES merged grid models**, a core regional service within the **Outage Planning Coordination (OPC)** process defined by **ENTSO-E** and monitored by **ACER** under the EU System Operation Guideline (SO GL - Regulation 2017/1485) and Electricity Regulation (EU 2019/943).
For each scenario timestamp it applies the TSOs' planned outages to the regional CGMES merged model, runs an N-1 security analysis with pypowsybl and reports the overloads that point to potential OPIs.

### Context & Regulation

In the European electricity grid, Transmission System Operators (TSOs) and Regional Coordination Centres (RCCs) must coordinate planned outages of cross-border grid assets, power plants, and major network elements to maintain operational security and grid stability.

* **OPC (Outage Planning Coordination):** The pan-European operational framework established to collect, merge, and evaluate asset outage schedules across multiple timeframes (Year-Ahead to Week-Ahead).
* **OPI (Outage Planning Incompatibility):** A situation where planned outages put operational security at risk.

### How it works

* **Timestamps:** Selected per time horizon: Year-Ahead, Month-Ahead or Week-Ahead. For Year-Ahead only the critical timestamps of each season are assessed.
* **Models:** The CGMES merged model of each timestamp is taken from the merging process.
* **Outages:** The TSOs' planned outages are matched to the model elements and applied to the model.
* **Validation:** The model must solve before and after the outages are applied.
* **N-1 analysis:** Agreed contingencies are simulated and the assessed elements are checked for overloads. Neighbouring areas outside the assessed region are kept only for realistic flows.
* **Results:** Flows, system balance, confirmed outages and overloads are stored in Elasticsearch. The model with outages applied can be exported.
* **Report:** A report per time horizon lists the overloads above the agreed thresholds.
