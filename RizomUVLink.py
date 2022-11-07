import os

from RizomUVLinkBase import CRizomUVLinkBase
from RizomUVLinkBase import CZEx

class CRizomUVLink(CRizomUVLinkBase):
    def __init__(self):
        super().__init__()
        self.port = None

    def RunRizomUV(self, exePath : str = None, port : int = None, connect : bool = True, wait : bool = True) -> int:
        """ Runs RizomUV, connect to the instance and wait for it to be ready
        
            RizomUV standalone version must be 2022.2 or later. 
            
            If RizomUV is already running, another instance will be ran
            and the existing one will be left untouched and will be disconnected
            from this object instance.
         
            returns:
                The TCP port number used by the RizomUV instance to communicate.
         """
        if exePath is None:
            exePath = self.RizomUVPath()
        if exePath is None:
            raise CZEx("RizomUV executable path not found. Re-installing RizomUV should fix this issue.")

        # define the TCP port used for communication
        if port == None:
            # search a free TCP port on the dynamic range
            for p in range(49152, 65534):
                if not self.TCPPortIsOpen(p):
                    self.port = p
                    break
                if p == 65533:
                    raise CZEx("No available TCP Port found. This shouldn't be the case. Might worth to check your firewall settings just in case.")
        else:
            if self.TCPPortIsOpen(port):
                raise CZEx("Port " + str(port) + " is already in use, please connect using another port")
            self.port = port
        
        # change current directory to the RizomUV executable directory
        os.chdir(os.path.dirname(exePath))

        # run RizomUV asynchronously
        import subprocess
        subprocess.Popen(exePath + " -id " + str(self.port))

        # connect the the instance
        if connect:
            self.Connect(self.port)
        
        # wait for RizomUV initialisation to complete
        if wait:
            # calling the method will force to wait for the RizomUV to be ready
            version = self.RizomUVVersion()

        return self.port
        
    def RizomUVPath(self) -> str:
        import platform
        if platform.system() == "Windows":
            return self.RizomUVWinPath()
        elif platform.system() == "Darwin":
            return "/Applications/RizomUV.app/Contents/MacOS/RizomUV" #TODO
        elif platform.system() == "Linux":
            return "/usr/bin/RizomUV" #TODO
        else:
            raise CZEx("Unsupported platform: " + platform.system())
    
    def RizomUVWinPath(self):
        return "C:\\Users\\arqui\\Documents\\rizom-uv\\RizomUVApp\\bin\\rizomuv.exe";

    def RizomUVWinPath_(self):
        """ Returns the path to the most recent version 
            of RizomUV.exe installed on the system using
            the windows registry.
        """
        import winreg
        keyPaths = ["SOFTWARE\\Rizom Lab\\RizomUV VS RS 2022.1",
                "SOFTWARE\\Rizom Lab\\RizomUV VS RS 2022.0"]

        for path in keyPaths:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                return winreg.QueryValue(key, "rizomuv.exe")
            except:
                pass
        return None
    