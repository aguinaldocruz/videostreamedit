"""Optional local PostgreSQL supervisor. Never upgrades an existing cluster."""
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, quote

PG_BIN = Path('/usr/lib/postgresql/18/bin')
stopping = False


def request_stop(*_):
    global stopping
    stopping = True


def run_pg(*args, **kwargs):
    return subprocess.run(['gosu','postgres',str(PG_BIN/args[0]),*args[1:]],check=True,**kwargs)


def main():
    if os.getuid()!=0:
        raise RuntimeError('The optional supervisor must start as root; services run unprivileged')
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,request_stop)
    subprocess.run(['/usr/local/bin/docker-entrypoint.sh','true'],check=True)
    from app import db_bootstrap
    from psycopg import sql
    url=db_bootstrap.effective_url()
    local=not url or urlsplit(url).hostname in ('127.0.0.1','localhost','::1')
    postgres=None
    app=None
    pgdata=Path(os.getenv('PGDATA','/data/postgres'))
    if not pgdata.is_absolute() or pgdata.resolve() in (Path('/'),Path('/data'),Path('/config'),Path('/backup')):
        raise RuntimeError('PGDATA must be a dedicated absolute database directory')
    try:
        if local:
            version=pgdata/'PG_VERSION'
            if version.exists() and version.read_text().strip()!='18':
                raise RuntimeError('Local PostgreSQL cluster is not version 18. Use logical backup/restore into a new directory; automatic upgrades are forbidden.')
            if not version.exists() and pgdata.exists() and any(pgdata.iterdir()):
                raise RuntimeError('Refusing to initialize a nonempty PostgreSQL directory')
            pgdata.mkdir(parents=True,exist_ok=True)
            Path('/run/postgresql').mkdir(exist_ok=True)
            subprocess.run(['chown','-R','postgres:postgres',str(pgdata),'/run/postgresql'],check=True)
            if not version.exists():
                run_pg('initdb','-D',str(pgdata),'--auth-local=peer','--auth-host=scram-sha-256','--encoding=UTF8',stdout=subprocess.DEVNULL)
            tz=os.getenv('TZ','America/Sao_Paulo')
            postgres=subprocess.Popen(['gosu','postgres',str(PG_BIN/'postgres'),'-D',str(pgdata),'-c','listen_addresses=127.0.0.1','-c','unix_socket_directories=/run/postgresql','-c',f'timezone={tz}','-c',f'log_timezone={tz}'])
            deadline=time.monotonic()+45
            while True:
                if stopping: return 0
                if postgres.poll() is not None: raise RuntimeError('Local PostgreSQL exited during startup')
                ready=subprocess.run(['gosu','postgres',str(PG_BIN/'pg_isready'),'-h','/run/postgresql'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
                if ready: break
                if time.monotonic()>deadline: raise RuntimeError('Local PostgreSQL readiness timed out')
                time.sleep(.25)
            if not url:
                password=secrets.token_urlsafe(36)
                # Administrator authentication is local peer only. SQL is sent
                # through stdin, not exposed as process-list arguments.
                script=sql.SQL('CREATE ROLE videostreamedit LOGIN PASSWORD {}; CREATE DATABASE videostreamedit OWNER videostreamedit;').format(sql.Literal(password)).as_string()
                run_pg('psql','-h','/run/postgresql','-d','postgres','-v','ON_ERROR_STOP=1',input=script,text=True,stdout=subprocess.DEVNULL)
                url='postgresql://videostreamedit:'+quote(password,safe='')+'@127.0.0.1:5432/videostreamedit'
                subprocess.run(['gosu','videostreamedit','python','-c','import sys; from app.db_bootstrap import save_url; save_url(sys.stdin.read())'],input=url,text=True,check=True)
                os.environ['DATABASE_URL']=url
        if stopping: return 0
        app=subprocess.Popen(['/usr/local/bin/docker-entrypoint.sh','uvicorn','app.v86:app','--host','0.0.0.0','--port','8080','--timeout-graceful-shutdown','90'])
        while not stopping and app.poll() is None:
            if postgres and postgres.poll() is not None:
                raise RuntimeError('Local PostgreSQL exited; stopping application safely')
            time.sleep(.25)
        return app.returncode if app.returncode is not None else 0
    finally:
        if app and app.poll() is None:
            app.terminate()
            try: app.wait(timeout=95)
            except subprocess.TimeoutExpired:
                app.kill(); app.wait()
        if postgres and postgres.poll() is None:
            # Fast shutdown rolls back unfinished transactions and checkpoints;
            # unlike SIGKILL, it leaves a clean, restartable cluster.
            run_pg('pg_ctl','-D',str(pgdata),'-m','fast','-w','-t','20','stop')
            postgres.wait(timeout=5)


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        # Never print a connection URL or generated password on failure.
        print('Optional database supervisor failed: '+type(exc).__name__,file=sys.stderr)
        if isinstance(exc,RuntimeError): print(str(exc),file=sys.stderr)
        sys.exit(1)
