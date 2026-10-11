"""Real FastAPI/SQLite integration, not a console fixture contract."""
from fastapi.testclient import TestClient
from ai_ops.app import create_app

TOKEN='visibility-test-admin-0000000000000000'
ALL='/api/v1/assets?include_retired=true&include_disabled_local=true'

def test_retired_card_and_disabled_local_restore_via_real_api(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_OPS_LOCAL_CONNECTOR_ENABLED','1')
    app=create_app(str(tmp_path/'control.db'),TOKEN)
    c=TestClient(app,headers={'Authorization':'Bearer '+TOKEN})
    aid=c.post('/api/v1/assets',json={'name':'retained-name','notes':'original note','allowed_users':['reader']}).json()['asset_id']
    local_id=c.get('/api/v1/local-connector').json()['asset_id']
    assert local_id
    assert c.post('/api/v1/assets/'+aid+'/retire',json={'note':'integration test'}).status_code==200
    assert c.post('/api/v1/local-connector/disable').status_code==200
    assert aid not in {r['id'] for r in c.get('/api/v1/assets').json()}
    assert local_id not in {r['id'] for r in c.get('/api/v1/assets').json()}
    rows={r['id']:r for r in c.get(ALL).json()}
    assert rows[aid]['retired_at'] is not None and rows[aid]['notes']=='original note'
    assert rows[aid]['local_enabled'] is None
    assert rows[local_id]['local_enabled'] is False
    assert rows[local_id]['retired_at'] is None
    assert rows[local_id]['health']['health_state']=='disabled'
    assert local_id not in {r['id'] for r in c.get('/api/v1/assets?include_retired=true').json()}
    assert aid not in {r['id'] for r in c.get('/api/v1/assets?include_disabled_local=true').json()}
    assert c.get('/api/v1/assets?include_retired=invalid').status_code==422
    assert TestClient(app).get(ALL).status_code==401
    # Editing the retained card writes the asset source, not a second document.
    assert c.put('/api/v1/assets/'+aid+'/notes',json={'name':'updated','notes':'edited'}).status_code==200
    assert c.post('/api/v1/assets/'+aid+'/unretire').status_code==200
    assert c.post('/api/v1/local-connector/enable').status_code==200
    rows={r['id']:r for r in c.get('/api/v1/assets').json()}
    assert rows[aid]['retired_at'] is None and rows[aid]['notes']=='edited'
    assert rows[local_id]['local_enabled'] is True
    assert 'updated' in c.get('/api/v1/documents/system-asset-inventory').text
    assert c.post('/api/v1/assets/'+aid+'/purge',json={'confirm_asset_id':'wrong'}).status_code==422
    assert aid in {r['id'] for r in c.get(ALL).json()}
    assert c.post('/api/v1/assets/'+aid+'/purge',json={'confirm_asset_id':aid}).status_code==200
    assert aid not in {r['id'] for r in c.get(ALL).json()}
    with app.state.transaction() as db:
        events={r[0] for r in db.execute('SELECT event FROM audit WHERE entity_id=?',(aid,))}
        assert {'asset.retired','asset.unretired','asset.purged'} <= events
