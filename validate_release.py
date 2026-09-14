"""Reproducible offline release checks. Does not start or deploy the bot."""
import argparse,hashlib,json,os,subprocess,sys,tempfile,time
from pathlib import Path
import release_manifest

ROOT=Path(__file__).resolve().parent

def source_hashes():
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.glob('*.py'))}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='release_evidence');parser.add_argument('--timeout',type=int,default=180);a=parser.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,BOT_TEST_MODE='1',MPLBACKEND='Agg',PYTHONUNBUFFERED='1')
    for key in ('API_MODE','LEARN_ENABLED','ANTHROPIC_API_KEY','ALPACA_API_KEY','ALPACA_API_SECRET','TELEGRAM_BOT_TOKEN'):
        env.pop(key,None)
    with tempfile.TemporaryDirectory(prefix='kelbot_offline_') as temp:
        # Enforced in every child interpreter, including child processes that
        # intentionally turn test mode off to test the transport's dry path.
        Path(temp,'sitecustomize.py').write_text('''import sys

def offline(event,args):
    if event == "socket.connect":
        address=args[1]
        if isinstance(address,tuple) and address[0] not in ("127.0.0.1","::1","localhost"):
            raise OSError("Release tests cannot access external networks")
    if event == "socket.getaddrinfo" and args[0] not in (None,"127.0.0.1","::1","localhost"):
        raise OSError("Release tests cannot resolve external hosts")
sys.addaudithook(offline)
''')
        env['PYTHONPATH']=temp+os.pathsep+str(ROOT)+os.pathsep+env.get('PYTHONPATH','')
        generated=subprocess.run([sys.executable,'release_manifest.py'],cwd=ROOT,env=env,capture_output=True,text=True)
        (out/'manifest_generation.log').write_text(generated.stdout+generated.stderr)
        if generated.returncode:return generated.returncode
        before=source_hashes();results=[]
        for name in release_manifest._gate_tests():
            start=time.monotonic()
            try:
                r=subprocess.run([sys.executable,name],cwd=ROOT,env=env,capture_output=True,text=True,errors='replace',timeout=a.timeout)
                body=r.stdout+r.stderr;code=r.returncode
            except subprocess.TimeoutExpired as e:
                body='Suite exceeded deadline; no success claimed.';code=124
            status='failed' if code else 'skipped_external_packet' if 'SKIP: no review packet' in body else 'passed'
            (out/(name+'.log')).write_text(body)
            results.append({'suite':name,'exit_code':code,'status':status,'seconds':round(time.monotonic()-start,3),'sha256':before.get(name)})
            (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
            print(name+': '+status,flush=True)
        unchanged=before==source_hashes()
        checked=subprocess.run([sys.executable,'release_manifest.py','--check'],cwd=ROOT,env=env,capture_output=True,text=True)
        (out/'manifest_check.log').write_text(checked.stdout+checked.stderr)
        summary={'passed':sum(r['status']=='passed' for r in results),'failed':sum(r['exit_code']!=0 for r in results),
                 'skipped':sum(r['status'].startswith('skipped') for r in results),'source_unchanged':unchanged,
                 'manifest_check_exit':checked.returncode,'python':sys.version,'external_network_blocked':True,
                 'source_hashes':before,'deployed':False}
        (out/'SUMMARY.json').write_text(json.dumps(summary,indent=2)+'\n')
        (out/'release_manifest.json').write_bytes(release_manifest.OUT.read_bytes())
        print(json.dumps({k:v for k,v in summary.items() if k!='source_hashes'}),flush=True)
        return int(summary['failed']>0 or not unchanged or checked.returncode!=0)
if __name__=='__main__':raise SystemExit(main())
