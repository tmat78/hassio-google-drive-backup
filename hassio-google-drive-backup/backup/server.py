import os.path
import os
import cherrypy
from datetime import timedelta
from datetime import datetime
from oauth2client.client import OAuth2WebServerFlow
from oauth2client.client import OAuth2Credentials
from .helpers import nowutc
from .helpers import formatTimeSince
from .helpers import formatException
from .engine import Engine
from .config import Config
from .knownerror import KnownError
from .logbase import LogBase
from typing import Dict, Any, Optional
from .snapshots import Snapshot
from cherrypy.lib.static import serve_file
from pathlib import Path

# Used to Google's oauth verification
SCOPE: str = 'https://www.googleapis.com/auth/drive.file'
MANUAL_CODE_REDIRECT_URI: str = "urn:ietf:wg:oauth:2.0:oob"


class Server(LogBase):
    """
    Add delete capabilities

    Make the website less sassy

    make cherrpy optionally use SSL

    Change the app credentials to use somethig more specific than philopen
    ADD Comments
    """
    def __init__(self, root: str, engine: Engine, config: Config):
        self.oauth_flow_manual: OAuth2WebServerFlow = None
        self.root: str = root
        self.engine: Engine = engine
        self.config: Config = config
        self.auth_cache: Dict[str, Any] = {}
        self.last_log_index = 0
        self.host_server = None
        self.ingress_server = None
        self.running = False

    @cherrypy.expose  # type: ignore
    @cherrypy.tools.json_out()  # type: ignore
    def getstatus(self) -> Dict[Any, Any]:
        status: Dict[Any, Any] = {}
        status['folder_id'] = self.engine.folder_id
        status['snapshots'] = []
        last_backup: Optional[datetime] = None
        for snapshot in self.engine.snapshots:
            if (last_backup is None or snapshot.date() > last_backup):
                last_backup = snapshot.date()
            details = None
            if snapshot.ha:
                details = snapshot.ha.source
            status['snapshots'].append({
                'name': snapshot.name(),
                'slug': snapshot.slug(),
                'size': snapshot.sizeString(),
                'status': snapshot.status(),
                'date': str(snapshot.date()),
                'inDrive': snapshot.isInDrive(),
                'inHA': snapshot.isInHA(),
                'isPending': snapshot.isPending(),
                'protected': snapshot.protected(),
                'type': snapshot.version(),
                'details': details,
                'deleteNextDrive': snapshot.deleteNextFromDrive,
                'deleteNextHa': snapshot.deleteNextFromHa,
                'driveRetain': snapshot.driveRetained(),
                'haRetain': snapshot.haRetained()
            })
        status['ask_error_reports'] = (self.config.sendErrorReports() is None)
        status['drive_snapshots'] = self.engine.driveSnapshotCount()
        status['ha_snapshots'] = self.engine.haSnapshotCount()
        status['restore_link'] = self.getRestoreLink()
        status['drive_enabled'] = self.engine.driveEnabled()
        status['cred_version'] = self.engine.credentialsVersion()
        status['warn_ingress_upgrade'] = self.config.warnExposeIngressUpgrade()
        status['ingress_url'] = self.engine.hassio.getIngressUrl()
        next: Optional[datetime] = self.engine.getNextSnapshotTime()
        if not next:
            status['next_snapshot'] = "Disabled"
        elif (next < nowutc()):
            status['next_snapshot'] = formatTimeSince(nowutc())
        else:
            status['next_snapshot'] = formatTimeSince(next)

        if last_backup:
            status['last_snapshot'] = formatTimeSince(last_backup)
        else:
            status['last_snapshot'] = "Never"

        status['last_error'] = self.engine.getError()
        status['last_exception'] = self.engine.getExceptionInfo()
        status["firstSync"] = self.engine.firstSync
        status["maxSnapshotsInHasssio"] = self.config.maxSnapshotsInHassio()
        status["maxSnapshotsInDrive"] = self.config.maxSnapshotsInGoogleDrive()
        status["retainDrive"] = self.engine.driveSnapshotCount() - self.engine.driveDeletableSnapshotCount()
        status["retainHa"] = self.engine.haSnapshotCount() - self.engine.haDeletableSnapshotCount()
        status["snapshot_name_template"] = self.config.snapshotName()
        if len(status['last_error']) > 0:
            status['debug_info'] = self.engine.getDebugInfo()
        return status

    def getRestoreLink(self):
        if self.config.useIngress():
            return "/hassio/snapshots"
        if not self.engine.hassio.ha_info:
            return ""
        if self.engine.hassio.ha_info['ssl']:
            url = "https://"
        else:
            url = "http://"
        url = url + "{host}:" + str(self.engine.hassio.ha_info['port']) + "/hassio/snapshots"
        return url

    @cherrypy.expose  # type: ignore
    @cherrypy.tools.json_out()
    def manualauth(self, code: str = "", client_id: str = "", client_secret: str = "") -> None:
        if client_id != "" and client_secret != "":
            try:
                # Redirect to the webpage that takes you to the google auth page.
                self.oauth_flow_manual = OAuth2WebServerFlow(
                    client_id=client_id.strip(),
                    client_secret=client_secret.strip(),
                    scope=SCOPE,
                    redirect_uri=MANUAL_CODE_REDIRECT_URI,
                    include_granted_scopes='true',
                    prompt='consent',
                    access_type='offline')
                return {
                    'auth_url': self.oauth_flow_manual.step1_get_authorize_url()
                }
            except Exception as e:
                return {
                    'error': "Couldn't create authorizatin URL, Google said:" + str(e)
                }
            raise cherrypy.HTTPError()
        elif code != "":
            try:
                self.engine.saveCreds(self.oauth_flow_manual.step2_exchange(code))
                if self.config.useIngress() and 'ingress_url' in self.engine.hassio.self_info:
                    return {
                        'auth_url': self.engine.hassio.self_info['ingress_url']
                    }
                else:
                    return {
                        'auth_url': "/"
                    }
            except Exception as e:
                return {
                    'error': "Couldn't create authorization URL, Google said:" + str(e)
                }
            raise cherrypy.HTTPError()

    def auth(self, realm: str, username: str, password: str) -> bool:
        if username in self.auth_cache and self.auth_cache[username]['password'] == password and self.auth_cache[username]['timeout'] > nowutc():
            return True
        try:
            self.engine.hassio.auth(username, password)
            self.auth_cache[username] = {'password': password, 'timeout': (nowutc() + timedelta(minutes=10))}
            return True
        except Exception as e:
            self.error(formatException(e))
            return False

    @cherrypy.expose  # type: ignore
    @cherrypy.tools.json_out()  # type: ignore
    def triggerbackup(self, custom_name=None, retain_drive=False, retain_ha=False) -> Dict[Any, Any]:
        retain_drive = self.strToBool(retain_drive)
        retain_ha = self.strToBool(retain_ha)
        try:
            for snapshot in self.engine.snapshots:
                if snapshot.isPending():
                    return {"error": "A snapshot is already in progress"}

            snapshot = self.engine.startSnapshot(custom_name=custom_name, retain_drive=retain_drive, retain_ha=retain_ha)
            return {"name": snapshot.name()}
        except KnownError as e:
            return {"error": e.message, "detail": e.detail}
        except Exception as e:
            return {"error": formatException(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def deleteSnapshot(self, slug: str, drive: str, ha: str) -> Dict[Any, Any]:
        delete_drive: bool = (drive == "true")
        delete_ha: bool = (ha == "true")
        try:
            if not delete_drive and not delete_ha:
                return {"message": "Bad request, gave nothing to delete"}
            self.engine.deleteSnapshot(slug, delete_drive, delete_ha)
            return {"message": "Its gone!"}
        except Exception as e:
            self.error(formatException(e))
            return {"message": "{}".format(e), "error_details": formatException(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def retain(self, slug, drive, ha):
        try:
            found: Optional[Snapshot] = None
            for snapshot in self.engine.snapshots:
                if snapshot.slug() == slug:
                    found = snapshot
                    break

            if not found:
                return {
                    'message': 'Snapshot couldn\'t be found',
                    'error_details': 'Snapshot couldn\'t be found'
                }
            self.engine.setRetention(found, self.strToBool(drive), self.strToBool(ha))
            return {
                'message': "Updated the snapshot's settings"
            }
        except Exception as e:
            self.error(formatException(e))
            return {
                'message': 'Failed to update snapshot\'s settings',
                'error_details': formatException(e)
            }

    def strToBool(self, value) -> bool:
        return str(value).lower() in ['true', 't', 'yes', 'y', '1', 'hai', 'si', 'omgyesplease']

    @cherrypy.expose
    def log(self, format="download", catchup=False) -> Any:
        if not catchup:
            self.last_log_index = 0
        if format == "view":
            return open("www/logs.html")
        if format == "html":
            cherrypy.response.headers['Content-Type'] = 'text/html'
        else:
            cherrypy.response.headers['Content-Type'] = 'text/plain'
            cherrypy.response.headers['Content-Disposition'] = 'attachment; filename="hassio-google-drive-backup.log"'

        def content():
            html = format == "colored"
            if format == "html":
                yield "<html><head><title>Hass.io Google Drive Backup Log</title></head><body><pre>\n"
            for line in self.getHistory(self.last_log_index, html):
                self.last_log_index = line[0]
                if line:
                    yield line[1].replace("\n", "   \n") + "\n"
            if format == "html":
                yield "</pre></body>\n"
        return content()

    @cherrypy.expose
    def token(self, **kwargs: Dict[str, Any]) -> None:
        if 'creds' in kwargs:
            creds = OAuth2Credentials.from_json(kwargs['creds'])
            self.engine.saveCreds(creds)
        if self.config.useIngress():
            return self.redirect("/hassio/ingress/" + self.engine.hassio.self_info['slug'])
        else:
            return self.redirect("/")

    @cherrypy.expose
    def simerror(self, error: str = "") -> None:
        if len(error) == 0:
            self.engine.simulateError(None)
        else:
            self.engine.simulateError(error)

    @cherrypy.expose
    def index(self) -> Any:
        if not self.engine.driveEnabled():
            return open("www/index.html")
        else:
            return open("www/working.html")

    @cherrypy.expose
    def pp(self):
        return open("www/privacy_policy.html")

    @cherrypy.expose
    def tos(self):
        return open("www/terms_of_service.html")

    @cherrypy.expose  # type: ignore
    def reauthenticate(self) -> Any:
        return open("www/index.html")

    def run(self) -> None:
        if self.running:
            self.info("Stopping server...")
            cherrypy.engine.stop()

        self.config.setIngressInfo(self.engine.hassio.host_info)

        # unbind existing servers.
        if self.host_server is not None:
            self.host_server.unsubscribe()
            self.host_server = None

        conf: Dict[Any, Any] = {
            'global': {
                'server.socket_port': self.config.ingressPort(),
                'server.socket_host': '0.0.0.0',
                'engine.autoreload.on': False,
                'log.access_file': '',
                'log.error_file': '',
                'log.screen': False,
                'response.stream': True
            },
            "/": {
                'tools.staticdir.on': True,
                'tools.staticdir.dir': os.getcwd() + self.config.pathSeparator() + self.root,
                'tools.auth_basic.on': self.config.requireLogin(),
                'tools.auth_basic.realm': 'localhost',
                'tools.auth_basic.checkpassword': self.auth,
                'tools.auth_basic.accept_charset': 'UTF-8'
            }
        }

        self.info("Starting server on port {}".format(self.config.ingressPort()))

        cherrypy.config.update(conf)

        if self.config.exposeExtraServer():
            self.info("Starting server on port {}".format(self.config.port()))
            self.host_server = cherrypy._cpserver.Server()
            self.host_server.socket_port = self.config.port()
            self.host_server._socket_host = "0.0.0.0"
            self.host_server.subscribe()
            if self.config.useSsl():
                self.host_server.ssl_certificate = self.config.certFile()
                self.host_server.ssl_private_key = self.config.keyFile()

        cherrypy.tree.mount(self, "/", conf)

        cherrypy.engine.start()
        self.info("Server started")
        self.running = True

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def backupnow(self) -> Any:
        self.engine.doBackupWorkflow()
        return self.getstatus()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def getconfig(self) -> Any:
        data = self.config.config.copy()
        data['addons'] = self.engine.hassio.readSupervisorInfo()['addons']
        data['support_ingress'] = self.config.useIngress()
        # get the latest list of add-ons
        return data

    @cherrypy.expose
    def errorreports(self, send: str) -> None:
        if send == "true":
            self.config.setSendErrorReports(self.engine.hassio.updateConfig, True)
        else:
            self.config.setSendErrorReports(self.engine.hassio.updateConfig, False)

    @cherrypy.expose
    def exposeserver(self, expose: str) -> None:
        if expose == "true":
            self.config.setExposeAdditionalServer(self.engine.hassio.updateConfig, True)
        else:
            self.config.setExposeAdditionalServer(self.engine.hassio.updateConfig, False)
        try:
            return {'redirect': self.engine.hassio.getIngressUrl()}
        finally:
            self.run()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def saveconfig(self, **kwargs) -> Any:
        try:
            use_ssl = self.config.useSsl()
            use_password = self.config.requireLogin()
            cert_file = self.config.certFile()
            key_file = self.config.keyFile()
            self.config.update(self.engine.hassio.updateConfig, **kwargs)
            if use_ssl != self.config.useSsl() or use_password != self.config.requireLogin() or cert_file != self.config.certFile() or key_file != self.config.keyFile():
                self.run()
            return {'message': 'Settings saved'}
        except Exception as e:
            return {
                'message': 'Failed to save settings',
                'error_details': formatException(e)
            }

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def upload(self, slug):
        try:
            found: Optional[Snapshot] = None
            for snapshot in self.engine.snapshots:
                if snapshot.slug() == slug:
                    found = snapshot
                    break

            if not found or not found.driveitem:
                raise cherrypy.HTTPError(404)

            if found.isDownloading():
                return {'message': "Snapshot is already being uploaded."}

            self.engine.doUpload(found)

            if not found.isInHA():
                return {'message': "Something went wrong, Hass.io didn't recognize the snapshot.  Please check the supervisor logs."}
            return {'message': "Snapshot uploaded"}
        except Exception as e:
            return {
                'message': 'Failed to Upload snapshot',
                'error_details': formatException(e)
            }

    def redirect(self, url):
        return Path("www/redirect.html").read_text().replace("{url}", url)

    @cherrypy.expose
    def download(self, slug):
        found: Optional[Snapshot] = None
        for snapshot in self.engine.snapshots:
            if snapshot.slug() == slug:
                found = snapshot
                break

        if not found or (not found.ha and not found.driveitem):
            raise cherrypy.HTTPError(404)

        if found.ha:
            return serve_file(
                os.path.abspath(os.path.join(self.config.backupDirectory(), found.slug() + ".tar")),
                "application/tar",
                "attachment",
                "{}.tar".format(found.name()))
        elif found.driveitem:
            cherrypy.response.headers['Content-Type'] = 'application/tar'
            cherrypy.response.headers['Content-Disposition'] = 'attachment; filename="{}.tar"'.format(found.name())
            cherrypy.response.headers['Content-Length'] = str(found.size())

            return self.engine.drive.download(found.driveitem.id(), int(found.size()))
        else:
            raise cherrypy.HTTPError(404)
