# MIT License
# 
# Copyright (c) [2026] [Rizom-Lab]
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import sys
import os
import tempfile

from os.path import dirname

def RizomUVWinRegisterInstallPath():
    """ Returns the path to the most recent version 
        of the RizomUV installation directory on the system using
        the Windows registry.
            
        Every "RizomUV VS RS <major>.<minor>" key from 2022.2 up is tried,
        highest version first. None when there is none.
    """
    import re
    import winreg

    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Rizom Lab")
    except FileNotFoundError:
        return None

    with root:
        versions = []
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:  # no more subkeys
                break
            index += 1
            match = re.fullmatch(r"RizomUV VS RS ([0-9]+)\.([0-9]+)", name)
            if match:
                # compared as integers, so 2027.10 ranks above 2027.2
                version = (int(match.group(1)), int(match.group(2)))
                if version >= (2022, 2):  # RizomUVLink ships since RizomUV 2022.2
                    versions.append((version, name))

        for version, name in sorted(versions, reverse=True):
            try:
                with winreg.OpenKey(root, name) as key:
                    exePath = winreg.QueryValue(key, "rizomuv.exe")
                return os.path.dirname(exePath)
            except FileNotFoundError:
                pass

    return None

# add the RizomUVLink installation path to the Python module search paths
sys.path.append(RizomUVWinRegisterInstallPath() + "/RizomUVLink")

# import all RizomUVLink module items.
# the correct .pyc binary library for the current Python version should be loaded.
# Current supported Python versions are 3.6 to 3.13.
# Please tell us if other Python version are needed
from RizomUVLink import *

# create a rizomuvlink object instance
link = CRizomUVLink()
print("RizomUVLink " + link.Version() + " instance has been created")

# Run rizomuv standalone and connect the link to it.
# The returned port is a free TCP port used to communicate with the two entities.
# Installed RizomUV Standalone must be version >= 2022.2
port = link.RunRizomUV()
print("RizomUV " + link.RizomUVVersion() + " is now listening commands on TCP port: " + str(port))

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #  
#                                                                             
# "link" is now associated to the current RizomUV standalone instance and     
# ready to send commands to it                                                
#                                                                             
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #  
#                                                                             
# The link object instance is meant to be persistant along with the           
# standalone instance. This permits to load new meshes and send new commands  
# without the need to run RizomUV again and wait for its initialisation.      
# So if you can, keep the link instance somewhere and use it as long as       
# possible along with the RizomUV standalone instance.                        
#                                                                             
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #  
#                                                                             
# In very special cases, several RizomUV instance can be ran simultaneously.  
# To do that simply create a new link instance and run a new RizomUV instance.
# The following link1 and link2 object can perfectly live simultaneously and  
# independently
#                                                                             
# link1 = CRizomUVLink()                                                      
# link2 = CRizomUVLink()                                                      
#                                                                             
# link1.RunRizomUV()                                                          
# link2.RunRizomUV()                                                          
#                                                                             
# link1.Load(...)                                                             
# link2.Load(...)                                                             
# link1.Unfold()                                                              
# link2.Unfold()                                                              
#                                                                             
#       .                                                                     
#       .                                                                     
#       .                                                                     
#                                                                             
#  link1.Quit()                                                               
#                                                                             
#  link2.Pack()                                                               
#  link2.Quit()                                                               
#                                                                             
# WARNING: While the previous lines are perfectly legal, each RizomUV instance
# take 1 token on floating license configuration. This is not a problem       
# in case of nodelocked licenses however, but in case of floating license     
# you could running out of license token.                                     
#                                                                             
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #  

try:
    # mesh path inpt & output from the example directory
    meshInputPath = dirname(__file__) + "/ExampleMesh.obj"
    meshOutputPath = tempfile.gettempdir() + "/ExampleMeshOutput.obj"

    params = {  
        "File.Path": meshInputPath,
        "File.XYZUVW": True,                    # 3D + UV data loaded (use File.XYZ instead to load 3D data only)
        "File.UVWProps": True,                  # UVs properties such as pinning, texel density settings etc... will be loaded
        "File.ImportGroups": True,              # Island group hierarchy will be loaded
        "__Focus": True,                        # Focus viewports on the loaded mesh
    }
    link.Load(params)

    # Unfold full mesh with default parameters
    link.Unfold({})
    
    # Pack full mesh
    link.Pack({"Translate": True})                             
    
    # Save the mesh with default parameters
    link.Save({"File.Path" : meshOutputPath})   

    # Close RizomUV standalone instance associated to link
    link.Quit({})

    # link.Load(...) # obviously won't work here as the rizomUV instance 
    # is closed so a new call to link.RunRizomUV() would be necessary 
    
except CZEx as ex:
    print(str(ex))

print("Done")