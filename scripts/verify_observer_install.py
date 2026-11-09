"""Verify motion against installed files and a running dashboard without private paths.
Usage: python3 scripts/verify_observer_install.py --installed-root /path/to/site-packages --url http://127.0.0.1:55050/
"""
import argparse, subprocess, urllib.request
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--installed-root',type=Path,required=True)
parser.add_argument('--url',required=True)
parser.add_argument('--ref',default='HEAD',help='Commit expected to supply the installed observer')
args=parser.parse_args();repo=Path(__file__).resolve().parents[1]
expected=subprocess.check_output(['git','show',args.ref+':harness/observer.html'],cwd=repo)
actual=(args.installed_root/'harness/observer.html').read_bytes()
assert actual==expected, 'Installed observer differs from the requested commit'
subprocess.run(['node',str(repo/'scripts/verify_observer_motion.cjs'),str(args.installed_root)],check=True)
with urllib.request.urlopen(args.url,timeout=5) as response: page=response.read().decode()
for token in ['class="header-tools"','@keyframes state-orbit-turn','id="pastBox"','const stateRings=new Map()']:
 assert token in page, 'Served observer is missing '+token
print('PASS installed observer, motion behaviour and served dashboard')
