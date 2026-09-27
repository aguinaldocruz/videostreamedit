"""Isolated optional-image boot/restart/shutdown and database restore rehearsal.

Never mounts live config, data or media. Backup directory is read-only. All test
databases and anonymous volumes are removed with the uniquely named container.
"""
import subprocess
import time
import uuid

name='vse-isolated-'+uuid.uuid4().hex[:12]


def docker(*args,**kw):
    return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,**kw).stdout


def ready():
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        if docker('inspect','-f','{{.State.Running}}',name).strip()!='true':
            logs=subprocess.run(['docker','logs',name],capture_output=True,text=True)
            raise RuntimeError('Test container exited: '+(logs.stdout+logs.stderr)[-6000:])
        result=subprocess.run(['docker','exec',name,'python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2)"],capture_output=True)
        if result.returncode==0: return
        time.sleep(.5)
    raise RuntimeError('Readiness timed out: '+docker('logs',name)[-4000:])


try:
    docker('run','-d','--name',name,'--network','none','--init',
           '--mount','type=bind,src=/home/docker/videostreamedit/backup,dst=/fixture-backup,readonly',
           'videostreamedit:all-in-one')
    ready()
    print('PASS: isolated fresh PostgreSQL 18 + application startup',flush=True)
    docker('exec',name,'gosu','postgres','createdb','-h','/run/postgresql','restore_rehearsal')
    docker('exec','-i',name,'python','-',input="""
import tarfile
from pathlib import Path
p=Path('/fixture-backup/videostreamedit-backup-20260927-154037.tar.gz')
with tarfile.open(p) as t:
    member=t.getmember('database.dump')
    with t.extractfile(member) as src, open('/tmp/restore.dump','wb') as dst:
        import shutil; shutil.copyfileobj(src,dst)
""")
    docker('exec',name,'gosu','postgres','pg_restore','-h','/run/postgresql','-d','restore_rehearsal','--no-owner','--no-acl','--exit-on-error','/tmp/restore.dump')
    counts=docker('exec',name,'gosu','postgres','psql','-h','/run/postgresql','-d','restore_rehearsal','-At','-c',
        'SELECT (SELECT count(*) FROM plex_media), (SELECT count(*) FROM track_name_correction_history), (SELECT count(*) FROM reusable_stream_values)')
    catalog,learned,saved=map(int,counts.strip().split('|'))
    assert catalog>0 and learned>0 and saved>0, counts
    print('PASS: full isolated database restore; catalog/learned/saved counts '+counts.strip(),flush=True)
    docker('stop','--time','125',name)
    assert docker('inspect','-f','{{.State.ExitCode}}',name).strip()=='0'
    assert 'database system is shut down' in docker('logs',name) or 'database system is shut down' in subprocess.run(['docker','logs',name],capture_output=True,text=True).stderr
    docker('start',name)
    ready()
    print('PASS: generated encrypted credentials reused after restart',flush=True)
    docker('stop','--time','125',name)
    assert docker('inspect','-f','{{.State.ExitCode}}',name).strip()=='0'
    print('PASS: graceful application/database shutdown',flush=True)
finally:
    # Exact throwaway container, never the production container or volumes.
    subprocess.run(['docker','rm','-f','-v',name],check=False,capture_output=True)
