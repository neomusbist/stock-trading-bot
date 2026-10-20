"""Validate the version catalog. No runtime import, network, broker or writes."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def ordered_versions(data):
    return sorted(data.get('versions',[]),key=lambda row:row['order'])


def validate(data,root=None,check_git=False):
    errors=[];versions=set();orders=set();commits=set();fingerprints=set()
    rows=data.get('versions')
    if not isinstance(rows,list):return ['VERSIONS_LIST_REQUIRED']
    for row in rows:
        version=row.get('version');order=row.get('order');status=row.get('status')
        if not isinstance(version,str) or not re.fullmatch(r'v\d+\.\d+',version):errors.append('VERSION_NUMBER_STRING_ONLY')
        if version in versions:errors.append('DUPLICATE_VERSION')
        versions.add(version)
        if type(order) is not int or order<0 or order in orders:errors.append('EXPLICIT_UNIQUE_ORDER_REQUIRED')
        orders.add(order)
        if status=='UNKNOWN':
            if row.get('sha') is not None:errors.append('UNKNOWN_MUST_NOT_INVENT_RELEASE_SHA')
            continue
        if status not in {'HISTORICAL_PRERELEASE','RELEASED'}:errors.append('UNKNOWN_RELEASE_STATUS')
        sha=row.get('sha');fp=row.get('runtime_source_fingerprint')
        if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{40}',sha):errors.append('EXACT_COMMIT_REQUIRED')
        if sha in commits:errors.append('SAME_COMMIT_MULTIPLE_VERSIONS')
        commits.add(sha)
        if not isinstance(fp,str) or not re.fullmatch('[0-9a-f]{64}',fp):errors.append('CODE_FINGERPRINT_REQUIRED')
        if fp in fingerprints:errors.append('SAME_CODE_MULTIPLE_VERSIONS')
        fingerprints.add(fp)
        if type(row.get('prerelease')) is not bool or row['prerelease']!=(status=='HISTORICAL_PRERELEASE'):
            errors.append('RELEASE_STATE_CONTRADICTION')
        if check_git and isinstance(sha,str):
            result=subprocess.run(['git','rev-parse','refs/tags/'+str(version)+'^{commit}'],cwd=root,
                capture_output=True,text=True)
            if result.returncode or result.stdout.strip()!=sha:errors.append('TAG_COMMIT_MISMATCH:'+str(version))
    current=[r for r in rows if r.get('version')==data.get('current_release')]
    if len(current)!=1 or current[0].get('status')!='RELEASED' or current[0].get('prerelease') is not False:
        errors.append('LATEST_MUST_BE_CONFIRMED_RELEASE')
    if data.get('next_major') in versions or data.get('next_patch') in versions:errors.append('PLANNED_VERSION_ALREADY_REGISTERED')
    if data.get('ordering')!='EXPLICIT_ORDER_NOT_FLOAT_OR_STRING_SORT':errors.append('EXPLICIT_ORDERING_REQUIRED')
    return sorted(set(errors))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check-git',action='store_true');args=parser.parse_args(argv)
    data=json.loads((args.root/'VERSION_INDEX.json').read_text(encoding='utf-8-sig'))
    errors=validate(data,args.root,args.check_git)
    print(json.dumps({'valid':not errors,'errors':errors,'catalog_current_release':data.get('current_release'),
        'ordered_versions':[row['version'] for row in ordered_versions(data)] if not errors else None,
        'runtime_version_or_approval_not_changed':True},ensure_ascii=False))
    return bool(errors)


if __name__=='__main__':raise SystemExit(main())
