"""Local Skill preview is text-only and read-only; bindings need explicit save."""
import json
import pytest
from test_turns import env
from ai_ops.context import skill_view


def preview(env,filename,content):
    return env[0].post('/api/v1/skills/import-preview',headers=env[1],json={'filename':filename,'content':content})


def test_markdown_exact_text_and_preview_no_write(env):
    text='\ufeff---\r\nname: "中文示例"\r\ndescription: >\r\n  第一行\r\n  第二行\r\n---\r\n# 原文\r\n仅正文\r\n'
    r=preview(env,'SKILL.md',text)
    assert r.status_code==200
    body=r.json();assert body['writes'] is False and body['scripts_installed'] is False
    assert body['requires_role_confirmation'] is True
    assert body['skills']==[{'name':'中文示例','description':'第一行 第二行','content':text}]
    with env[2].transaction() as db:assert db.execute('SELECT count(*) FROM skills').fetchone()[0]==0


def test_bundle_drops_id_bindings_and_permissions(env):
    text=json.dumps({'skills':[{'id':'import-id','name':'One','content':'ONE','role_ids':['ops'],'enabled':True,'permissions':['root']},{'name':'Two','content':'TWO'}]})
    rows=preview(env,'bundle.json',text).json()['skills']
    assert rows==[{'name':'One','content':'ONE','description':''},{'name':'Two','content':'TWO','description':''}]
    c,h,*_=env
    r=c.post('/api/v1/skills',headers=h,json={'name':rows[0]['name'],'content':rows[0]['content'],'role_ids':[]})
    assert r.status_code==201,r.text
    item=r.json();assert item['id']!='import-id'
    with env[2].transaction() as db:
        assert skill_view(db,'ops')==[] and skill_view(db,'other')==[]
    assert c.put('/api/v1/skills/'+item['id'],headers=h,json={**item,'role_ids':['ops']}).status_code==200
    with env[2].transaction() as db:
        assert len(skill_view(db,'ops'))==1 and skill_view(db,'other')==[]
        assert db.execute('SELECT count(*) FROM authority_events').fetchone()[0]==0
    assert c.delete('/api/v1/skills/'+item['id'],headers=h).status_code==200
    with env[2].transaction() as db:
        assert skill_view(db,'ops')==[]
        assert db.execute('SELECT deleted_at FROM skills WHERE id=?',(item['id'],)).fetchone()[0] is not None


@pytest.mark.parametrize('filename,content', [('bad.json','{bad'),('empty.json','[]'),('missing.json','{"name":"bad"}'),('bad.md','---\nname: unfinished'),('file.zip','fixture-only'),('script.py','# no execution')])
def test_invalid_import_rejected(env,filename,content):
    assert preview(env,filename,content).status_code==422
    with env[2].transaction() as db:assert db.execute('SELECT count(*) FROM skills').fetchone()[0]==0


def test_oversized_unauthenticated_and_invalid_role(env):
    assert preview(env,'big.md','x'*200001).status_code==422
    assert env[0].post('/api/v1/skills/import-preview',json={'filename':'a.md','content':'ok'}).status_code in (401,403)
    r=env[0].post('/api/v1/skills',headers=env[1],json={'name':'bad-role','content':'x','role_ids':['missing-role']})
    assert r.status_code==404
    with env[2].transaction() as db:assert db.execute('SELECT count(*) FROM skills').fetchone()[0]==0
