## Overview

This project implements functionality for **Outage Planning Incompatibility (OPI)** assessment, a core regional service within the **Outage Planning Coordination (OPC)** process defined by **ENTSO-E** and monitored by **ACER** under the EU System Operation Guideline (SO GL - Regulation 2017/1485) and Electricity Regulation (EU 2019/943).

### Context & Regulation

In the European electricity grid, Transmission System Operators (TSOs) and Regional Coordination Centres (RCCs) must coordinate planned outages of cross-border grid assets, power plants, and major network elements to maintain operational security and grid stability.

* **OPC (Outage Planning Coordination):** The pan-European operational framework established to collect, merge, and evaluate asset outage schedules across multiple timeframes (Year-Ahead to Week-Ahead).
* **OPI (Outage Planning Incompatibility):** A specific assessment within the OPC workflow. An OPI occurs when two or more scheduled outages (e.g., simultaneous maintenance on adjacent transmission lines or generation units) jeopardize operational security, reduce cross-zonal transmission capacity below safe thresholds, or create critical grid constraints.

### Key Functionality

* **Incompatibility Detection:** Identifies conflicting outage schedules submitted by neighboring TSOs across Year-Ahead (Y-1), Month-Ahead (M-1) and Week-Ahead (W-1) timeframes.
* **KPI & Performance Monitoring:** Tracks process metrics defined by ACER/ENTSO-E guidelines (such as execution duration, process failure rates, and identified incompatibility counts).
* **Resolution Coordination Support:** Highlights conflicting network elements to facilitate operational alignment and re-scheduling between TSOs and Regional Coordination Centres.