"""Room feedback and adaptive timing regressions with deterministic device observations."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_energy_planner.climate_inputs import read_climate_inputs
from custom_components.ha_energy_planner.climate_runtime import economic_actions
from custom_components.ha_energy_planner.config_flow import OptionsFlow, _validate_options
from custom_components.ha_energy_planner.const import DEFAULT_OPTIONS
from custom_components.ha_energy_planner.models import DecisionContext, DecisionSlot, InputHealth, OccupancyState
from custom_components.ha_energy_planner.planner_hvac import HVACPlanningPolicy
from custom_components.ha_energy_planner.thermal_learning import rate, sample, summary, timing, update
from custom_components.ha_energy_planner.zone_temperature import (
    bounds,
    comfort_breached,
    control_temperature,
    feedback_missing,
    target_reached,
)

NOW = datetime(2026, 10, 18, 0, tzinfo=UTC)
ZONE = 'climate.main'


def context():
    c = DecisionContext(NOW, 'rooms', [
        DecisionSlot(NOW + timedelta(minutes=i * 5), 0.1 if i < 36 else 0.5, 0.05, 0.0, 1.0,
                     outdoor_temperature_forecast_c=30)
        for i in range(60)
    ], None, None, OccupancyState.OCCUPIED, InputHealth.HEALTHY,
        current_hvac_mode='cool', current_hvac_temperature_c=19, current_hvac_power_kw=1,
        current_outdoor_temperature_c=30, occupied_temperature_low_c=19, occupied_temperature_high_c=25)
    c.climate_zone_entities = [ZONE]
    c.climate_inputs = {'zones': {ZONE: {'temperature': 24.0, 'temperature_entity': 'sensor.room'}},
                        'power_source_type': 'estimated'}
    return c


def point(minutes=0, temp=24, mode='cool', **kwargs):
    at = NOW + timedelta(minutes=minutes)
    return {'at': at.isoformat(), 'learning_enabled': True, 'mode': mode, 'power': 1.0,
            'zones': {ZONE: 'cool'}, 'sources': {ZONE: {'identity': 'room-v1', 'sensor': 'sensor.room',
                'temperature': temp, 'reported_at': at.isoformat()}}, **kwargs}


def mature(mode='cool', value=1.0):
    return {'model_version': 3, 'learning_enabled': True, 'sources': {ZONE: {'buckets': {
        mode: {'count': 12, 'minutes': 60, 'last_at': NOW.isoformat(), 'rate_c_per_hour': value},
    }}}}


def test_room_feedback_uses_all_rooms_and_keeps_head_separate():
    c = context()
    c.climate_inputs['zones']['climate.other'] = {'temperature': 20.0, 'low': 18, 'high': 24}
    assert control_temperature(c, 'cool') == 24
    assert control_temperature(c, 'heat') == 20
    assert c.current_hvac_temperature_c == 19
    assert not target_reached(c, 'cool', 19)
    assert not target_reached(c, 'heat', 25)
    c.climate_inputs['zones'][ZONE]['temperature'] = 19
    c.climate_inputs['zones']['climate.other']['temperature'] = 18
    assert target_reached(c, 'cool', 19)
    assert not comfort_breached(c, 'cool', coasting=False)
    c.climate_inputs['zones']['climate.other']['temperature'] = 17.5
    assert comfort_breached(c, 'cool', coasting=False)
    c.climate_inputs['zones']['climate.other']['temperature'] = 25
    assert comfort_breached(c, 'heat', coasting=False)
    assert comfort_breached(c, 'cool', coasting=True)
    c.climate_inputs['zones'][ZONE]['temperature'] = 26
    assert target_reached(c, 'heat', 25)
    c.climate_inputs['zones'][ZONE]['temperature'] = None
    assert feedback_missing(c) and control_temperature(c) is None
    assert not target_reached(c, 'cool', 19)
    assert economic_actions(c, dict(DEFAULT_OPTIONS), []) == []
    c.hvac_control = {'phase': 'preconditioning'}
    action = economic_actions(c, dict(DEFAULT_OPTIONS), [])[0]
    assert action.desired_state['release_reason'] == 'hvac_required_evidence_lost'
    policy = HVACPlanningPolicy(DEFAULT_OPTIONS, {})
    assert policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0].kind == 'release_hvac'
    c.hvac_control = {}
    assert policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5)) == []


def test_unmapped_feedback_and_missing_bounds():
    c = context()
    c.climate_inputs = {}
    assert control_temperature(c) == 19
    assert target_reached(c, 'cool', 19)
    assert not target_reached(c, 'heat', 25)
    assert not comfort_breached(c, 'cool', coasting=True)
    c.current_hvac_temperature_c = 26
    assert comfort_breached(c, 'cool', coasting=True)
    c.current_hvac_temperature_c = None
    assert not target_reached(c, 'cool', 19)
    assert not comfort_breached(c, 'cool', coasting=False)
    c.climate_inputs = {'zones': {ZONE: {'temperature': 23}}}
    c.occupied_temperature_low_c = c.occupied_temperature_high_c = None
    assert bounds(c, {}) == (None, None)
    assert target_reached(c, 'cool', 24)
    assert not comfort_breached(c, 'heat', coasting=False)


@pytest.mark.parametrize(('unit','value','expected'), [('°C','23',23),('°F','77',25),('K','300',None)])
def test_mapped_units_and_freshness(unit, value, expected):
    state = SimpleNamespace(state=value, attributes={'unit_of_measurement': unit}, last_updated=NOW)
    states = {'sensor.room': state, ZONE: SimpleNamespace(state='cool')}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get), config=SimpleNamespace(currency='AUD'))
    data = {'climate_zone_entities': [ZONE], 'hvac_zone_mappings': {ZONE: {'temperature': 'sensor.room'}}}
    result = read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert result['zones'][ZONE]['temperature'] == expected
    assert result['zones'][ZONE]['temperature_entity'] == 'sensor.room'
    state.last_updated = NOW - timedelta(hours=3)
    result = read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert result['zones'][ZONE]['temperature'] is None
    states.pop('sensor.room')
    result = read_climate_inputs(hass, data, DEFAULT_OPTIONS, NOW, NOW + timedelta(hours=12), {})
    assert result['zones'][ZONE]['temperature'] is None


def test_learning_window_readiness_and_independent_modes():
    model = update({}, point())
    for i in range(1, 13):
        model = update(model, point(i * 5, 24 - i * .1))
    assert rate(model, ZONE, 'cool', NOW + timedelta(hours=1)) == pytest.approx(1.2)
    assert rate(model, ZONE, 'heat', NOW + timedelta(hours=1)) is None
    assert not model['active_hvac_load_kw']['sample_count']  # estimated watts never learn electrical load
    assert summary(model, NOW + timedelta(hours=1))['sources'][ZONE]['modes']['cool']['ready']
    assert rate(model, ZONE, 'cool', NOW + timedelta(days=15)) is None
    model = update(model, point(66, learning_enabled=False))
    assert not model['enabled']
    assert rate(model, ZONE, 'cool', NOW + timedelta(minutes=66)) is None


def test_coarse_readings_accumulate_and_keep_flat_and_adverse_windows():
    model = update({}, point())
    model = update(model, point(5, 23))
    assert not model['sources'][ZONE]['buckets']
    model = update(model, point(10, 23))
    assert model['sources'][ZONE]['buckets']['cool']['rate_c_per_hour'] == 6
    model = update(model, point(15, 23))
    assert model['sources'][ZONE]['anchor']['at'] == point(10)['at']
    model = update(model, point(40, 23))
    assert model['sources'][ZONE]['buckets']['cool']['windows'][-1]['rate'] == 0
    model = update(model, point(45, 23.1))
    assert model['sources'][ZONE]['buckets']['cool']['windows'][-1]['rate'] < 0


@pytest.mark.parametrize(('change','reason'), [
    ({'mode':'heat'}, 'operating_transition'),
    ({'zones':{ZONE:'off'}}, 'operating_transition'),
    ({'power':0}, 'power_transition'),
    ({'intervention':True}, 'intervention_or_uncertain_command'),
    ({'mode':'dry'}, 'unsupported_mode'),
    ({'learning_enabled':False}, 'learning_disabled'),
])
def test_learning_rejects_invalid_operating_windows(change, reason):
    model = update({}, point())
    model = update(model, point(5, **change))
    assert model['sources'][ZONE]['last_rejection'] == reason
    assert model['sources'][ZONE]['anchor'] is None
    model = update(model, point(10))
    assert not model['sources'][ZONE]['buckets']


def test_learning_missing_reports_gaps_bad_rates_and_source_reset():
    model = update({'model_version':2, 'enabled':True}, point())
    assert model['model_version'] == 3 and not model['enabled']
    assert update(model, point(31))['sources'][ZONE]['last_rejection'] == 'sampling_gap'
    assert update(model, point(0))['sources'][ZONE]['last_rejection'] == 'sampling_gap'
    bad = point(5)
    bad['sources'][ZONE]['temperature'] = None
    assert update(model, bad)['sources'][ZONE]['last_rejection'] == 'invalid_sensor'
    bad = point(5)
    bad['sources'][ZONE]['reported_at'] = None
    assert update(model, bad)['sources'][ZONE]['last_rejection'] == 'invalid_sensor'
    bad = point(5)
    bad['sources'][ZONE]['reported_at'] = point()['at']
    assert update(model, bad)['sources'][ZONE]['anchor'] == model['sources'][ZONE]['anchor']
    assert update(model, point(30, 40))['sources'][ZONE]['last_rejection'] == 'implausible_rate'
    model = update(model, point(5, 23.9, power_source_type='measured'))
    assert model['sources'][ZONE]['loads_kw'] == [{'power':1,'at':point(5)['at']}]
    new = point(10)
    new['sources'][ZONE]['identity'] = 'new'
    model = update(model, new)
    assert model['sources'][ZONE]['buckets'] == {}
    assert update(model, {'at': NOW.isoformat(), 'sources':{}})['sources'] == {}


@pytest.mark.parametrize('mode', ['heat','cool','off'])
def test_rolling_bound_and_passive_learning(mode):
    model = {}
    for i in range(101):
        model = update(model, point(i * 5, 20 + i * (.1 if mode != 'cool' else -.1), mode=mode,
                                    power_source_type='measured'))
    bucket = model['sources'][ZONE]['buckets'][mode if mode != 'off' else 'passive']
    assert bucket['count'] == 96 and len(bucket['windows']) == 96
    assert bucket['minutes'] == 480


def test_adaptive_lead_and_partial_progress():
    c = context()
    model = mature(value=2)
    result = timing(c, DEFAULT_OPTIONS, 'cool', model)
    assert result['effective_lead_minutes'] == 150
    assert result['zones'][ZONE]['predicted_finishing_temperature'] == 19
    model = mature(value=1)
    result = timing(c, DEFAULT_OPTIONS, 'cool', model)
    assert result['effective_lead_minutes'] == 180 and result['partial_preconditioning']
    assert result['zones'][ZONE]['shortfall_c'] == 2
    disabled = {**DEFAULT_OPTIONS, 'hvac_thermal_learning_enabled':False}
    assert timing(c, disabled, 'cool', model)['effective_lead_minutes'] == 90
    disabled = {**DEFAULT_OPTIONS, 'hvac_precondition_lead_minutes':0}
    assert timing(c, disabled, 'cool', model)['effective_lead_minutes'] == 0
    assert timing(c, DEFAULT_OPTIONS, 'cool', {})['effective_lead_minutes'] == 90
    c.climate_inputs['thermal_model'] = model
    candidate = HVACPlanningPolicy(DEFAULT_OPTIONS, model)._next_hvac_period(c)
    assert candidate is not None
    assert candidate['precondition_start'] == NOW
    assert candidate['precondition_end'] == NOW + timedelta(hours=3)
    assert c.climate_legacy_decision['thermal_timing']['partial_preconditioning']


def test_sampling_captures_fresh_report_and_uncertain_command():
    c = context()
    states = SimpleNamespace(get=lambda entity: SimpleNamespace(last_updated=NOW, state='cool'))
    result = sample(c, DEFAULT_OPTIONS, states, 'climate.head')
    assert result['sources'][ZONE]['reported_at'] == NOW.isoformat()
    c.hvac_control = {'phase':'preconditioning'}
    assert sample(c, DEFAULT_OPTIONS, states, 'climate.head')['intervention']
    c.climate_inputs = {}
    c.active_overrides = [SimpleNamespace(kind='manual_hvac')]
    assert 'head' in sample(c, DEFAULT_OPTIONS, states, 'climate.head')['sources']
    missing = sample(c, DEFAULT_OPTIONS, SimpleNamespace(get=lambda _: None), 'climate.head')
    assert missing['sources']['head']['reported_at'] is None


def test_zone_temperature_options_preserve_mappings_and_actuators():
    original = {'climate_zone_entities': [ZONE], 'hvac_zone_mappings': {ZONE:{'humidity':'sensor.humidity'}}}
    entry = SimpleNamespace(data=original, options={'hvac_precondition_lead_minutes':60})
    updates = []
    flow = OptionsFlow(entry)
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity: SimpleNamespace(
        attributes={'unit_of_measurement':'°C'})), config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kwargs: updates.append(kwargs)))
    form = asyncio.run(flow.async_step_zone_temperatures())
    assert form['step_id'] == 'zone_temperatures'
    result = asyncio.run(flow.async_step_zone_temperatures({ZONE:'sensor.room'}))
    assert result['type'] == 'create_entry'
    assert updates[-1]['data']['hvac_zone_mappings'][ZONE] == {'humidity':'sensor.humidity','temperature':'sensor.room'}
    assert entry.data == original and result['data']['hvac_precondition_lead_minutes'] == 60
    assert asyncio.run(flow.async_step_zone_temperatures({ZONE:'switch.bad'}))['errors'][ZONE] == 'invalid_unit'
    asyncio.run(flow.async_step_zone_temperatures({}))
    assert updates[-1]['data']['hvac_zone_mappings'][ZONE] == {'humidity':'sensor.humidity'}
    flow._data['hvac_zone_mappings'][ZONE] = {'temperature':'sensor.room'}
    asyncio.run(flow.async_step_zone_temperatures({}))
    assert updates[-1]['data']['hvac_zone_mappings'] == {}
    flow._data = {}
    assert asyncio.run(flow.async_step_zone_temperatures())['reason'] == 'no_climate_zones'


@pytest.mark.parametrize(('initial','maximum'), [(90,80),(float('nan'),180),(90,float('inf')),(90,361)])
def test_invalid_lead_limits(initial, maximum):
    options = {**DEFAULT_OPTIONS,'hvac_precondition_lead_minutes':initial,'hvac_precondition_max_lead_minutes':maximum}
    assert 'hvac_precondition_max_lead_minutes' in _validate_options(options)
    assert DEFAULT_OPTIONS['hvac_precondition_lead_minutes'] == 90


def test_empty_learning_clock_blank_mapping_and_nonpositive_rate():
    from custom_components.ha_energy_planner.thermal_model import thermal_active_temperature_rate_c_per_hour
    assert update({}, {})["sources"] == {}
    c = context()
    old = {"model_version":3, "enabled":True,
           "active_cool_rate_c_per_hour":{"average":-1,"sample_count":12}}
    assert thermal_active_temperature_rate_c_per_hour(old,"cool") is None
    flow = OptionsFlow(SimpleNamespace(data={"climate_zone_entities":[ZONE]}, options={}))
    assert asyncio.run(flow.async_step_zone_temperatures({}))["type"] == "create_entry"
    policy = HVACPlanningPolicy(DEFAULT_OPTIONS, {})
    policy._project_active_hvac_slots(c, phase="peak_coast", mode="cool", active_until=NOW,
                                     comfort_boundary=25)
    policy._project_active_hvac_slots(c, phase="preconditioning", mode="cool",
                                     active_until=NOW + timedelta(hours=10), comfort_boundary=25)
    assert all(slot.projected_hvac_load_kw > 0 for slot in c.slots)


@pytest.mark.parametrize(('state','power','provenance','expected'), [
    ('off',1,'estimated','off'), ('unavailable',1,'estimated',None),
    ('unknown',1,'estimated',None), ('cool',0,'measured','idle'), ('cool',1,'measured','cool'),
])
def test_sampling_distinguishes_zone_off_and_measured_compressor_idle(state, power, provenance, expected):
    c = context()
    c.current_hvac_power_kw = power
    c.climate_inputs['power_source_type'] = provenance
    states = SimpleNamespace(get=lambda entity: SimpleNamespace(state=state,last_updated=NOW))
    result = sample(c, DEFAULT_OPTIONS, states, 'climate.head')
    assert result['sources'][ZONE]['mode'] == expected
    model = update({}, result)
    assert model['sources'][ZONE]['buckets'] == {}


def test_transition_within_a_short_window_is_not_reused_and_clock_jitter_is_bounded():
    model = update({}, point())
    model = update(model, point(1, mode='heat'))
    assert model['sources'][ZONE]['last_rejection'] == 'operating_transition'
    assert update(update({}, point()), point(30.1))['sources'][ZONE]['buckets']['cool']['count'] == 1
    assert update(update({}, point()), point(31))['sources'][ZONE]['last_rejection'] == 'sampling_gap'


def test_source_change_preserves_unaffected_room_history_and_passive_projection():
    p = point()
    p['sources']['climate.other'] = {**p['sources'][ZONE], 'identity':'other'}
    model = update({}, p)
    for i in range(1,13):
        p = point(i * 5, 24 - i * .1)
        p['sources']['climate.other'] = {**p['sources'][ZONE], 'identity':'other'}
        model = update(model, p)
    other = deepcopy(model['sources']['climate.other']['buckets'])
    p = point(65)
    p['sources'][ZONE]['identity'] = 'replacement'
    p['sources']['climate.other'] = {**p['sources'][ZONE], 'identity':'other', 'reported_at':point(60)['at']}
    model = update(model, p)
    assert model['sources'][ZONE]['buckets'] == {}
    assert model['sources']['climate.other']['buckets'] == other
    c = context()
    model = mature(value=2)
    model['sources'][ZONE]['buckets']['passive'] = {
        'count':12, 'minutes':60, 'last_at':NOW.isoformat(), 'rate_c_per_hour':1,
    }
    assert timing(c, DEFAULT_OPTIONS, 'cool', model, delay_minutes=60)['required_minutes'] == 180
    result = timing(c, DEFAULT_OPTIONS, 'cool', model, available_minutes=30)
    assert result['partial_preconditioning']
    assert result['zones'][ZONE]['predicted_finishing_temperature'] == 23
    c.climate_inputs['zones'][ZONE]['temperature'] = 20
    model = mature(mode='heat',value=2)
    result = timing(c, DEFAULT_OPTIONS, 'heat', model)
    assert result['effective_lead_minutes'] == 150
    assert result['zones'][ZONE]['predicted_finishing_temperature'] == 25


def test_owned_cycle_uses_room_completion_instead_of_cold_head_feedback():
    c = context()
    policy = HVACPlanningPolicy(DEFAULT_OPTIONS, {})
    actions = policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    c.hvac_control = {**actions[0].desired_state, 'main_state_committed':True}
    assert c.current_hvac_temperature_c == 19  # Head has reached the target; room has not.
    actions = policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert actions[0].desired_state['phase'] == 'preconditioning'
    c.climate_inputs['zones'][ZONE]['temperature'] = 19
    actions = policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))
    assert actions[0].desired_state['phase'] == 'pre_peak_coast'
    c.climate_inputs['zones'][ZONE]['temperature'] = 18.5
    assert policy._hvac_lifecycle_actions(c, NOW, NOW + timedelta(minutes=5))[0].desired_state[
        'release_reason'] == 'hvac_comfort_handoff'


@pytest.mark.parametrize('mode', ['heat','cool'])
def test_future_passive_drift_can_extend_preparation_within_the_maximum(mode):
    c = context()
    c.current_hvac_mode = mode
    c.climate_inputs['zones'][ZONE]['temperature'] = 24 if mode == 'cool' else 20
    model = mature(mode=mode,value=2)
    model['sources'][ZONE]['buckets']['passive'] = {
        'count':12,'minutes':60,'last_at':NOW.isoformat(),'rate_c_per_hour':1 if mode == 'cool' else -1,
    }
    result = timing(c, DEFAULT_OPTIONS, mode, model, deadline_minutes=240)
    assert result['effective_lead_minutes'] == 180
    assert result['zones'][ZONE]['predicted_finishing_temperature'] == (19 if mode == 'cool' else 25)
    assert not result['partial_preconditioning']
    assert timing(c, DEFAULT_OPTIONS, mode, model, deadline_minutes=60)['required_minutes'] == 150
    for i, slot in enumerate(c.slots):
        slot.import_price = .1 if i < 48 else .5
        slot.outdoor_temperature_forecast_c = 30 if mode == 'cool' else 10
    candidate = HVACPlanningPolicy(DEFAULT_OPTIONS, model)._next_hvac_period(c)
    assert candidate['precondition_start'] == NOW + timedelta(hours=1)
    assert candidate['precondition_end'] == NOW + timedelta(hours=4)


def test_passive_gain_as_fast_as_active_does_not_divide_by_zero():
    c = context()
    model = mature(value=1)
    model['sources'][ZONE]['buckets']['passive'] = {
        'count':12,'minutes':60,'last_at':NOW.isoformat(),'rate_c_per_hour':-1,
    }
    result = timing(c, DEFAULT_OPTIONS, 'cool', model, deadline_minutes=360)
    assert result['effective_lead_minutes'] == 180
    assert result['zones'][ZONE]['predicted_finishing_temperature'] == 19


@pytest.mark.parametrize('mode', ['heat','cool'])
def test_review_blocks_takeover_at_an_opposite_room_comfort_boundary(mode):
    c = context()
    c.current_hvac_mode = mode
    c.climate_inputs['zones'][ZONE]['temperature'] = 24 if mode == 'cool' else 20
    c.climate_inputs['zones']['climate.other'] = {
        'temperature_entity':'sensor.other', 'temperature':18 if mode == 'cool' else 26,
    }
    for slot in c.slots:
        slot.outdoor_temperature_forecast_c = 30 if mode == 'cool' else 10
    assert HVACPlanningPolicy(DEFAULT_OPTIONS, {})._next_hvac_period(c) is None
    assert c.climate_legacy_decision['reason'] == 'zone_comfort_boundary'


@pytest.mark.parametrize('helper', ['low','high'])
def test_review_missing_room_comfort_helpers_block_instead_of_falling_back(helper):
    states = {'sensor.room': SimpleNamespace(state='20.5',attributes={'unit_of_measurement':'°C'},last_updated=NOW),
              'input_number.room_limit':SimpleNamespace(state='unavailable',attributes={},last_updated=NOW),
              ZONE:SimpleNamespace(state='cool')}
    data = {'climate_zone_entities':[ZONE], 'hvac_zone_mappings':{ZONE:{
        'temperature':'sensor.room', helper:'input_number.room_limit',
    }}}
    c = context()
    c.climate_inputs = read_climate_inputs(SimpleNamespace(states=SimpleNamespace(get=states.get)), data,
                                          DEFAULT_OPTIONS,NOW,NOW+timedelta(hours=12),{})
    assert feedback_missing(c)
    assert bounds(c,c.climate_inputs['zones'][ZONE])[0 if helper == 'low' else 1] is None
    assert HVACPlanningPolicy(DEFAULT_OPTIONS,{})._next_hvac_period(c) is None
    c.hvac_control = {'phase':'preconditioning'}
    assert economic_actions(c,DEFAULT_OPTIONS,[])[0].kind == 'release_hvac'


def test_review_extended_lead_qualifies_against_the_extended_price_window():
    c = context()
    for i, slot in enumerate(c.slots):
        slot.import_price = .1 if i < 18 else .2 if i < 36 else .35
    candidate = HVACPlanningPolicy(DEFAULT_OPTIONS,mature(value=1))._next_hvac_period(c)
    assert candidate is not None
    assert candidate['precondition_start'] == NOW
    assert candidate['period_start'] == NOW+timedelta(hours=3)


@pytest.mark.parametrize(('interval','maximum'), [(25,180),(30,175),(60,175),(60,30)])
def test_review_grid_rounding_never_exceeds_the_maximum_lead(interval,maximum):
    c = context()
    c.slots = [DecisionSlot(NOW+timedelta(minutes=interval*i),.1 if i < 8 else .5,.05,0,1,
                            outdoor_temperature_forecast_c=30) for i in range(20)]
    options = {**DEFAULT_OPTIONS,'planning_interval_minutes':interval,
               'hvac_precondition_lead_minutes':min(90,maximum),'hvac_precondition_max_lead_minutes':maximum}
    candidate = HVACPlanningPolicy(options,mature(value=.5))._next_hvac_period(c)
    if maximum < interval:
        assert candidate is None
    else:
        assert candidate is not None
        assert (candidate['period_start']-candidate['precondition_start']).total_seconds()/60 <= maximum
        assert (candidate['precondition_end']-candidate['precondition_start']).total_seconds()/60 <= maximum


@pytest.mark.parametrize('mode', ['heat','cool'])
@pytest.mark.parametrize('room_target', [False,True])
def test_review_qualified_rates_do_not_start_a_cycle_when_every_room_is_at_target(mode,room_target):
    c = context()
    c.current_hvac_mode = mode
    target = (23 if mode == 'cool' else 21) if room_target else (19 if mode == 'cool' else 25)
    c.climate_inputs['zones'][ZONE]['temperature'] = target
    if room_target:
        c.climate_inputs['zones'][ZONE]['low' if mode == 'cool' else 'high'] = target
    model = mature(mode=mode,value=1)
    model.update(enabled=True,passive_indoor_drift_c_per_hour={'average':0,'sample_count':12})
    model['sources'][ZONE]['buckets']['passive'] = {
        'count':12,'minutes':60,'last_at':NOW.isoformat(),'rate_c_per_hour':0,
    }
    for slot in c.slots:
        slot.outdoor_temperature_forecast_c = 30 if mode == 'cool' else 10
    assert HVACPlanningPolicy(DEFAULT_OPTIONS,model)._next_hvac_period(c) is None


def test_review_one_fresh_window_cannot_resurrect_expired_evidence():
    model = update({},point())
    for i in range(1,13):
        model = update(model,point(i*5,24-i*.1,power_source_type='measured'))
    after_expiry = 15*24*60
    assert rate(model,ZONE,'cool',NOW+timedelta(minutes=after_expiry)) is None
    for i in range(3):
        model = update(model,point(after_expiry+i*5,20-i*.05,power_source_type='measured'))
    assert rate(model,ZONE,'cool',NOW+timedelta(minutes=after_expiry+10)) is None
    assert model['sources'][ZONE]['buckets']['cool']['count'] == 1
    assert model['active_hvac_load_kw']['sample_count'] == 1


def test_review_zone_selector_rejects_more_mappings_than_the_reader_supports():
    zones = [f'climate.zone_{i}' for i in range(17)]
    flow = OptionsFlow(SimpleNamespace(data={'climate_zone_entities':zones},options={}))
    changes = []
    flow.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _:SimpleNamespace(
        attributes={'unit_of_measurement':'°C'})), config_entries=SimpleNamespace(
            async_update_entry=lambda *a,**k:changes.append(k)))
    result = asyncio.run(flow.async_step_zone_temperatures({zone:'sensor.room' for zone in zones}))
    assert result['type'] == 'form' and result['errors']['base'] == 'invalid_climate_configuration'
    assert not changes


def test_review_invalid_explicit_mapping_cannot_revert_to_head_feedback():
    c = context()
    data = {'climate_zone_entities':[ZONE], 'hvac_zone_mappings':{
        ZONE:{'temperature':'sensor.room','unsupported':'sensor.other'},
    }}
    c.climate_inputs = read_climate_inputs(SimpleNamespace(states=SimpleNamespace(get=lambda _:None)),data,
                                          DEFAULT_OPTIONS,NOW,NOW+timedelta(hours=12),{})
    assert c.climate_inputs['configuration_valid'] is False
    assert feedback_missing(c) and control_temperature(c) is None
    assert HVACPlanningPolicy(DEFAULT_OPTIONS,{})._hvac_lifecycle_actions(c,NOW,NOW+timedelta(minutes=5)) == []
    assert c.climate_legacy_decision['reason'] == 'zone_temperature_unavailable'
    c.hvac_control = {'phase':'preconditioning'}
    assert economic_actions(c,DEFAULT_OPTIONS,[])[0].kind == 'release_hvac'


def test_review_crossed_room_comfort_helpers_fail_closed():
    c = context()
    c.climate_inputs['zones'][ZONE].update(low=23,high=22)
    assert feedback_missing(c)
    assert not target_reached(c,'cool',23)


def test_review_mixed_age_windows_recompute_readiness_and_diagnostics():
    model = update({},point())
    for i in range(1,13):
        model = update(model,point(i*5,24-i*.1,power_source_type='measured'))
    later = NOW+timedelta(days=14,minutes=10)
    bucket = summary(model,later)['sources'][ZONE]['modes']['cool']
    assert not bucket['ready'] and bucket['count'] == 11
    assert rate(model,ZONE,'cool',later) is None
    assert summary(model,later+timedelta(days=1))['sources'][ZONE]['modes']['cool']['count'] == 0
    old_loads = model['sources'][ZONE]['loads_kw']
    old_loads.extend([1,{'at':'invalid','power':99},{'at':(later+timedelta(days=1)).isoformat(),'power':99}])
    result = update(model,point(14*24*60+10,20))
    assert result['active_hvac_load_kw']['sample_count'] == 11
    assert result['active_hvac_load_kw']['average'] == 1


def test_second_review_old_report_is_not_a_recent_temperature_change():
    p = point()
    p['sources'][ZONE]['reported_at'] = (NOW-timedelta(hours=1)).isoformat()
    model = update({},p)
    model = update(model,point(10,23))
    assert not model['sources'][ZONE]['buckets']
    model = update(model,point(20,22.9))
    assert model['sources'][ZONE]['buckets']['cool']['rate_c_per_hour'] == pytest.approx(.6)


def test_second_review_rates_use_sensor_observation_time():
    model = update({},point())
    p = point(10,23.9)
    p['sources'][ZONE]['reported_at'] = (NOW+timedelta(minutes=6)).isoformat()
    model = update(model,p)
    assert model['sources'][ZONE]['buckets']['cool']['rate_c_per_hour'] == pytest.approx(1)
    assert model['sources'][ZONE]['buckets']['cool']['minutes'] == 6


def test_second_review_learning_off_preserves_qualified_measured_load():
    from custom_components.ha_energy_planner.thermal_model import thermal_hvac_load_kw
    model = update({},point(power=6,power_source_type='measured'))
    for i in range(1,13):
        model = update(model,point(i*5,24-i*.1,power=6,power_source_type='measured'))
    options = {**DEFAULT_OPTIONS,'hvac_thermal_learning_enabled':False}
    model = update(model,point(65,22.7,learning_enabled=False))
    policy = HVACPlanningPolicy(options,model)
    assert thermal_hvac_load_kw(policy.thermal_model,1) == 6
    assert summary(model,NOW+timedelta(minutes=65))['electrical_load_model_ready']
    assert not summary(model,NOW+timedelta(days=15))['electrical_load_model_ready']
    c = context()
    assert timing(c,options,'cool',model)['effective_lead_minutes'] == 90


def test_second_review_coast_uses_each_rooms_drift_and_bounds():
    c = context()
    c.climate_inputs['zones'][ZONE].update(temperature=23.5,low=23,high=24)
    other = 'climate.other'
    c.climate_inputs['zones'][other] = {'temperature_entity':'sensor.other','temperature':23.5,'low':23,'high':24}
    model = mature(value=6)
    model.update(enabled=True,active_cool_rate_c_per_hour={'average':6,'sample_count':12},
                 passive_indoor_drift_c_per_hour={'average':.1,'sample_count':12})
    model['sources'][ZONE]['buckets']['passive'] = {
        'count':12,'minutes':60,'last_at':NOW.isoformat(),'rate_c_per_hour':.1}
    model['sources'][other] = {'buckets':deepcopy(model['sources'][ZONE]['buckets'])}
    model['sources'][other]['buckets']['passive']['rate_c_per_hour'] = 3
    for i,slot in enumerate(c.slots):
        slot.import_price = .05 if i < 25 else .1 if i < 36 else .5
    policy = HVACPlanningPolicy(DEFAULT_OPTIONS,model)
    candidate = policy._next_hvac_period(c)
    assert candidate is not None
    assert (candidate['period_start']-candidate['precondition_end']).total_seconds()/3600 <= 1/3
    for slot in c.slots:
        slot.projected_hvac_load_kw = 0
    policy._project_hvac_coast_slots(c,mode='cool',coast_started_at=NOW,active_until=NOW+timedelta(hours=2),
                                    starting_temperature=23.5,comfort_boundary=25)
    assert c.slots[2].projected_hvac_load_kw > 0  # Warmest room reaches its own 24 C ceiling in ten minutes.


def test_second_review_delayed_coarse_reports_still_cover_thirty_minutes():
    model = update({},point())
    initial = point(2)
    initial['sources'][ZONE]['reported_at'] = NOW.isoformat()
    # Replay a normal late coordinator read of the initial anchor.
    model['sources'][ZONE]['anchor']['observed_at'] = initial['at']
    delayed = point(32,23)
    delayed['sources'][ZONE]['reported_at'] = (NOW+timedelta(minutes=30)).isoformat()
    model = update(model,delayed)
    assert model['sources'][ZONE]['buckets']['cool']['rate_c_per_hour'] == 2
    assert model['sources'][ZONE]['buckets']['cool']['minutes'] == 30


@pytest.mark.parametrize(('mode','outdoor','expected_load'), [('heat',10,True),('cool',30,True),('heat',30,True)])
def test_second_review_room_coast_uses_safe_fallback_when_drift_is_unavailable(mode,outdoor,expected_load):
    c = context()
    c.current_outdoor_temperature_c = outdoor
    c.climate_inputs['zones'][ZONE].update(temperature=21,low=20,high=22)
    policy = HVACPlanningPolicy(DEFAULT_OPTIONS,{})
    policy._project_active_hvac_slots(c,phase='peak_coast',mode=mode,active_until=NOW+timedelta(hours=4),
                                      comfort_boundary=19 if mode == 'heat' else 25)
    assert bool(c.slots[24].projected_hvac_load_kw) is expected_load
    c.occupied_temperature_high_c = None
    c.climate_inputs['zones'][ZONE].pop('high')
    policy._project_hvac_coast_slots(c,mode='cool',coast_started_at=NOW,active_until=NOW+timedelta(hours=2),
                                    starting_temperature=21,comfort_boundary=25)
    assert c.slots[0].projected_hvac_load_kw > 0


def test_second_review_report_gaps_short_windows_and_clock_regressions():
    model = update({},point())
    p = point(1,23.9)
    assert not update(model,p)['sources'][ZONE]['buckets']
    model['sources'][ZONE]['anchor']['observed_at'] = point(20)['at']
    result = update(model,point(40,23))
    assert not result['sources'][ZONE]['buckets']
    assert result['sources'][ZONE]['last_rejection'] == 'sensor_observation_gap'
    model = update({},point())
    model['sources'][ZONE]['anchor']['reported_at'] = None
    assert update(model,point(5,23.9))['sources'][ZONE]['last_rejection'] == 'sensor_observation_gap'
    p = point(5)
    p['sources'][ZONE]['reported_at'] = (NOW-timedelta(minutes=1)).isoformat()
    assert update(update({},point()),p)['sources'][ZONE]['last_rejection'] == 'invalid_sensor'


@pytest.mark.parametrize('inactive_mode', ['off',None])
@pytest.mark.parametrize('inactive_first', [True,False])
def test_third_review_shared_power_is_independent_of_room_order(inactive_mode,inactive_first):
    from custom_components.ha_energy_planner.thermal_model import thermal_hvac_load_kw
    model = {}
    for i in range(13):
        p = point(i*5,24-i*.1,power=6,power_source_type='measured')
        inactive = {**deepcopy(p['sources'][ZONE]),'identity':'inactive',
                    'temperature':22 if inactive_mode else None,'mode':inactive_mode}
        main = p['sources'][ZONE]
        p['sources'] = ({'climate.inactive':inactive,ZONE:main} if inactive_first
                        else {ZONE:main,'climate.inactive':inactive})
        model = update(model,p)
    assert model['active_hvac_load_kw'] == {'average':6,'sample_count':12}
    assert thermal_hvac_load_kw(model,1) == 6
    assert summary(model,NOW+timedelta(hours=1))['electrical_load_model_ready']


def test_third_review_shared_power_samples_are_not_duplicated_by_rooms():
    model = {}
    for i in range(13):
        p = point(i*5,24-i*.1,power=6,power_source_type='measured')
        p['sources']['climate.other'] = {**deepcopy(p['sources'][ZONE]),'identity':'other'}
        model = update(model,p)
    assert all(len(row['loads_kw']) == 12 for row in model['sources'].values())
    assert model['active_hvac_load_kw']['sample_count'] == 12
    assert not summary(model,NOW+timedelta(days=15))['electrical_load_model_ready']


def test_third_review_staggered_load_windows_keep_spacing_and_bound():
    from custom_components.ha_energy_planner.thermal_learning import _load_windows
    model = {'sources':{
        source:{'loads_kw':[{'power':6,'at':(NOW+timedelta(minutes=i*5+offset)).isoformat()} for i in range(96)]}
        for source,offset in [('main',0),('other',2.5)]
    }}
    rows = _load_windows(model,NOW+timedelta(hours=8))
    assert len(rows) == 96
    times = [datetime.fromisoformat(row['at']) for row in rows]
    assert all(right-left >= timedelta(minutes=5) for left,right in zip(times,times[1:],strict=False))
    assert rows[-1]['at'] == (NOW+timedelta(minutes=477.5)).isoformat()
    assert not _load_windows(model,NOW+timedelta(days=15))


@pytest.mark.parametrize("base", [240, 360])
@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("evidence", ["unready", "opposite_mode", "learning_disabled", "ready"])
def test_extended_base_lead_preserves_directional_readiness(base, mode, evidence):
    c = context()
    model = mature(mode=mode if evidence != "opposite_mode" else ("cool" if mode == "heat" else "heat"))
    if evidence == "unready":
        model['sources'][ZONE]['buckets'][mode]['count'] = 11
    options = {**DEFAULT_OPTIONS, 'hvac_precondition_lead_minutes': base,
               'hvac_precondition_max_lead_minutes': base,
               'hvac_thermal_learning_enabled': evidence != "learning_disabled"}
    details = timing(c, options, mode, model)
    assert details['effective_lead_minutes'] == base
    assert details['maximum_lead_minutes'] == base
    assert details['rates_ready'] is (evidence == "ready")
    assert details['finishing_temperature_known'] is (evidence == "ready")
    if evidence != "ready":
        assert details['required_minutes'] is None
        assert details['preparation_basis'] == 'configured_fallback'
        assert details['zones'][ZONE]['predicted_finishing_temperature'] is None
