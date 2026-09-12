import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from f916.config import Settings
from f916.publisher import Publisher


def test_publisher_rejects_tampered_or_external_evidence(tmp_path):
    settings=Settings(data_dir=tmp_path,github_repo='erku/erku-audit')
    (tmp_path/'github-deploy-ed25519').write_text('secret')
    (tmp_path/'github_known_hosts').write_text('host key')
    outside=tmp_path.parent/'outside-evidence.json'; outside.write_text('{}')
    with pytest.raises(ValueError):
        Publisher(settings).publish({'hash':hashlib.sha256(b'{}').hexdigest(),'evidence_files':[str(outside)]})
    root=tmp_path/'artifacts'/'run'; root.mkdir(parents=True); evidence=root/'evidence.json'; evidence.write_text('{}')
    with pytest.raises(ValueError):
        Publisher(settings).publish({'hash':'0'*64,'evidence_files':[str(evidence)]})


def test_publisher_uses_fixed_git_arguments_and_returns_immutable_url(tmp_path):
    evidence_dir=tmp_path/'artifacts'/'run'; evidence_dir.mkdir(parents=True)
    evidence=evidence_dir/'evidence.json'; evidence.write_bytes(b'{"safe":true}\n')
    digest=hashlib.sha256(evidence.read_bytes()).hexdigest(); calls=[]
    (tmp_path/'github-deploy-ed25519').write_text('secret')
    (tmp_path/'github_known_hosts').write_text('host key')
    repo=tmp_path/'public-repo'; (repo/'.git').mkdir(parents=True)
    def runner(args,**kwargs):
        calls.append(args)
        if args[:3]==['git','diff','--cached']: return SimpleNamespace(returncode=1,stdout='',stderr='')
        if args[:3]==['git','log','-1']: return SimpleNamespace(returncode=0,stdout='a'*40+'\n',stderr='')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    result=Publisher(Settings(data_dir=tmp_path,github_repo='erku/erku-audit'),runner=runner).publish(
        {'hash':digest,'evidence_files':[str(evidence)]})
    assert ['git','push','origin','HEAD:main'] in calls
    assert result['commit']=='a'*40
    assert '/blob/'+'a'*40+'/artifacts/'+digest+'.json' in result['public_url']
