# HVAC demand assessment — 26 October 2026

## Decision and evidence boundaries

Read-only inspection found Home Assistant 2026.10.4 running Energy Planner
**v1.4.7** (release commit `9b44eb9`). At **14:45:08 AEDT** the fallback selected
**heat to 24°C**, reporting `existing_equipment_mode`. Main measured **21.52°C**
and Bedrooms **19.48°C**, against a **21–24°C** comfort band. The equipment was
already heating under normal control, with Main disabled and Bedrooms enabled
before acquisition. That actuator state did not establish a need to preheat Main.

The decision audit and recorded tariff state support this reconstruction. The
broader source inventory was captured at **14:55:32 AEDT**, after the decision;
its weather, power and readiness observations are context, not an exact archived
input snapshot for 14:45. All times below are Australia/Melbourne (UTC+11).
No credentials, raw prompts, personal tracker history or location details are
part of the replay or this report.

## Available inputs and their decision roles

| Input | Observed evidence | Appropriate use and limits |
| --- | --- | --- |
| Mapped Main and Bedrooms temperatures | 21.52°C and 19.48°C at acquisition; mapped sensors `sensor.average_living_area_temperature` and `sensor.average_bedroom_temperature` were ready in subsequent diagnostics | Main is within its comfort band; Bedrooms needs comfort warming. One cold room does not justify heating both rooms to 24°C. |
| Comfort goals and zone topology | Shared low/high 21/24°C; independent Main, Bedrooms and both-zone operation configured safe; cooling equipment offsets Main −2°C, Bedrooms 0°C | Normal comfort controls can heat Bedrooms alone toward 21°C. Cooling equipment compensation is not a different room goal or permission to heat Main. |
| Current head/zone state and normal schedules | Head heating before takeover, head setpoint 19°C, Bedrooms equipment target 20°C; Main Off/Bedrooms On; four normal climate schedules enabled | State describes existing control and restoration baseline. It is not a forecast of room need and must not select a tariff preheating direction. |
| Tariff forecast | Recorded state at the 14:44 query boundary: 4.69¢/kWh at 15:00 and 15:30; 25.80¢ at 16:00; rising to 39.87¢ at 19:30 and 39.86¢ at 20:00; 21.76¢ at 21:00 | Qualifies shifting necessary demand before the expensive period. A price spread does not establish thermal demand. Query time is not publication time. |
| Outdoor weather | Later observation rainy, 13.3°C; latest hourly forecast 16°C at 14:00–17:00, then 15/14/13/12°C at 18:00/19:00/20:00/21:00 | Suggests possible later heating demand. It does not establish indoor heat loss or an indoor temperature at 16:00. Forecast timestamps were naive local times in HA's configured zone. Exact decision-time forecast unavailable. |
| Solar and irradiance | Later PV production 3.264kW and irradiance 740W/m²; daily solar forecast 38.19kWh today and 28.62kWh tomorrow | Solar can lower the cost of useful consumption. Daily totals are not remaining energy and irradiance is not a calibrated room heat gain. |
| Half-hour solar forecast | Later median forecast 3.72kW at 15:00, 2.89kW at 16:00, 1.46kW at 17:00, 0.79kW at 18:00 and near zero by 19:30; wide uncertainty bounds | Supports energy availability estimates with uncertainty, not a heating direction. It cannot prove that HVAC consumption would otherwise be curtailed. |
| Battery, grid and household load | Battery 100%; configured capacity 10kWh, reserve 20%, charge/discharge 7kW, round-trip efficiency 95%; import/export limits 14.5/10kW; later consumption 4.826kW | Constrains energy feasibility and cost. Configuration is not proof of measured capacity; asynchronous PV/load readings do not establish instantaneous surplus. |
| Household forecast and power freshness | Nine training days, full horizon coverage, upper-load coverage 91.8%, ready; six-hour outage grace configured; earlier recovery awaited advancing source samples | Supports conservative site-load planning. Does not qualify a room thermal model. Outage fallback and recovery remain unchanged. |
| Temperature learning | Enabled, but relevant room heating/cooling/passive rates unready in captured diagnostics; retained historical windows include other regimes; decision finishing/peak temperatures unknown | Retained counts are not usable rates. Windows spanning interventions, operating transitions and gaps were rejected. Unknown projections must stay unknown. Prospective-regime selection is a separate local fix, not included here. |
| HVAC electricity/economic model | Powercalc estimate, no qualified measured electrical load model; economic engine learning, zero qualified validation windows | No measured energy or savings claim and no authority to bypass fallback demand checks. Keep economic readiness/recovery requirements unchanged. |
| Occupancy and humidity | Two configured people home; Main humidity 55%; no maximum humidity policy configured | Occupancy enables ordinary comfort consideration, not a speculative 24°C target. No qualified humidity-driven acquisition is established. |
| Export control | Later zero-export confirmed with feed-in price about −1.25¢/kWh; battery profile AI Optimisation | Avoiding paid export may affect the cost of necessary HVAC operation. It cannot create room demand. Existing export/profile authority remains unchanged. |
| EV | Connected, `SUSPENDED_EV`, switch Off, power 0W; reported SOC 75%, target 80% | Respect vehicle refusal and reserve site capacity appropriately. Refusal does not prove target SOC; no charging retry or climate justification follows from it. |
| Overrides, command feedback and cycle limits | At 14:47:10 external/unattributed Off feedback interrupted acquisition and manual override became On; minimum cycle 20 minutes; acquisition awaiting feedback before interruption | No new acquisition while override is active. Do not attribute the change to a person without evidence. Preserve ownership, confirmation, restoration and minimum-cycle rules. |
| Optional sources | No arrival, forecast irradiance, per-room humidity/presence/comfort helper or COP table configured; AI advisory is advisory only | Missing inputs cannot supply evidence. No raw AI output was used to authorise control. |

Main rose from about 19.65°C at 11:00 to 21.52°C at 14:45; Bedrooms rose from
18.73°C to 19.48°C. Heating and solar exposure were present during this interval.
Those changes cannot be interpreted as passive room-loss rates or a reliable
counterfactual with heating disabled.

## Independent decision versus the deployed selection

At 14:45, **do not acquire the shared HVAC for automatic whole-unit preheating**.
Main was comfortable and had no qualified projection of crossing its lower
boundary. Bedrooms supported local comfort heating toward 21°C, which the normal
controls can provide using the confirmed safe Bedrooms-only topology. There was
also no supported need to cool Main at 21.52°C against the 21–24°C band.

The tariff spread, full battery and available solar can make necessary heating
cheaper. They cannot demonstrate that heat to 24°C is necessary, beneficial or
recoverable. The unvalidated economic model cannot establish otherwise. Once the
manual override was active, the planner must leave control with the operator or
normal automations until that authority changes.

The deployed v1.4.7 selection **did not align**: `existing_equipment_mode` bypassed
the room-demand assessment. Removing only idle-weather inference had left that
bypass available whenever another controller was already heating.

## Local correction and verification criteria

- Require actual room-boundary demand or qualified recent passive room evidence
  for every new Automatic fallback acquisition, regardless of head mode. Keep
  conflicting needs under normal comfort control. Explicit Cooling only remains
  an operator-selected preparation policy with existing comfort safeguards.
- Report observed equipment mode and evaluated peak time as diagnostic context,
  without treating mode as demand. Do not infer mapped indoor drift/coast from
  an outdoor difference or reuse a global equipment drift for mapped rooms.
- Recheck both heating and cooling demand before queued fallback acquisition.
  Preserve confirmed continuation of the same owned lifecycle, target completion,
  independent-zone rules, stops, original restoration and minimum-cycle limits.
- Preserve economic simulation/validation, cost/recovery and measured-energy
  requirements. Weather remains useful when a qualified model links it to room
  response; this patch does not manufacture that qualification.

`tests/fixtures/climate/demand_oct_26_2026.json` retains the sanitized decision and
recorded tariff snapshot. `tests/test_hvac_demand.py` rejects its former heat-to-24
selection and repeats the check across opposite head modes, extreme outdoor
weather and abundant energy availability. It also permits genuine all-room
heating/cooling demand and qualified passive predictions, rejects stale queued
acquisitions and fabricated coast, and preserves override/occupancy authority.
The corrected replay returns `conflicting_room_demand`, matching the independent
assessment. Companion tests cover learning, timing, equipment compensation and
ownership behavior. Full local acceptance is `scripts/docker-validate.sh`.

This is a code and evidence assessment, not live deployment or proof of better
physical performance. After installation, verify the decision reason on the next
qualifying run, correct room/zone behavior and confirmed restoration. Assess room
response and savings only with subsequent qualifying observations.
