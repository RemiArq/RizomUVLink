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

link = CRizomUVLink()

port = link.RunRizomUV()
print("Ran RizomUV with listening on port: " + str(port))

try:
    # Geometry definition
    
    # Polygons's size
    # In that cube example all polygons are quad, so each polygon has 4 attached vertexes.
    # In a case of a mesh that would be composed by mixes polygons,
    # you would have to set the polygons size accordingly to the number of vertexes they are attached to.
    polySizes = [4, 4, 4, 4, 4, 4]
    
    # # # # # 3D SPACE # # # #
    #
    #	 7________ 6
    #    /_______/|
    #  3|	   2| |
    #	|   p0 	| |5
    #   |_______|/
    #  0        1
    #
    # 3D space polygons Ids
    poly3DIDs = [0, 1, 2, 3, # p0
    3, 2, 6, 7,
    7, 6, 5, 4,
    4, 5, 1, 0,
    1, 5, 6, 2,
    0, 3, 7, 4]
    
    # 3D space coordinates
    xyzs = [0, 0, 0,	
    1, 0, 0,	
    1, 1, 0,	
    0, 1, 0,	
    0, 0, 1,	
    1, 0, 1,	
    1, 1, 1,	
    0, 1, 1]
    
    # # # # # UVW SPACE # # # # #
    # 
    #			1_______0
    #			|		|
    #			|  p0	|
    #	11______|2______|3______8
    #	|		|		|		|
    #	|	p4	|  p1	|	p5	|
    #	|12_____|_______|_____13|
    #		   6|	   7|
    #			|  p2	|
    #			|_______|
    #		   5|	   4|
    #			|  p3	|
    #		  10|______9|
    # UVW space polygons Ids
    # The list corresponds to a cut version of the cube.
    # That list implicitly defines the "seams" of the 3D original version 
    # of the cube.
    polyUVWIDs = [0, 1,  2, 3,		# p0
    3, 2,  6, 7,		# p1
    7, 6,  5, 4,		# p2
    4, 5,  10, 9,		# p3
    11,12, 6, 2,		# p4
    8, 3,  7, 13]		# p5
    #
    # UVW space Coordinates are taken from the 3D space ones
    # 
    # It is not a good idea to put zeroed coordinates as input
    # for the unfold / optimize algorithms since the UVW space 
    # polygon areas are taken into account to determine the size
    # of the unfolded islands.
    # 
    # Since at this step one don t already have the UV coordinates
    # one can use the 3D coordinates and copy them into
    # the cut version of the cube. That is the better option
    # for making a zom_new Unfolding from scratch.
    uvws = [0, 0, 0, 
    1, 0, 0, 
    1, 1, 0, 
    0, 1, 0, 
    0, 0, 1, 
    1, 0, 1, 
    1, 1, 1, 
    0, 1, 1, 
    0, 0, 0, 
    0, 0, 0, 
    1, 0, 0, 
    1, 0, 0, 
    1, 0, 1, 
    0, 0, 1]
    
    params = {"Data.PolySizes" : polySizes,
        "Data.PolyXYZIDs" : poly3DIDs,
        "Data.CoordsXYZ" : xyzs,
        "Data.PolyUVWIDs" : polyUVWIDs,
        "Data.CoordsUVW" : uvws,
        "__Focus": True,                # Focus viewports on the loaded mesh
    }
    link.Load(params)
    
    # Cube unwrap
    link.Unfold({})                     # Unfold full mesh with default parameters
    
    # Ask for exporting data into "output" variable as arrays
    params = {"Data": True}
    output = link.Save(params)
    
    # Get the polygons's node count (they are the same has imported, but provided here for convenience)
    print(output["Data"]["PolySizes"])

    # Get the zom_new UVW space polygons. 
    # When triangulation is specified at input the list is the same as imported
    # (if of course NO "cut" nor "welding" operations has been made).
    print(output["Data"]["PolyUVWIDs"])
    
    # The new UVW coordinates.
    print(output["Data"]["CoordsUVW"])

    # Exit RizomUV with success exit code 
    link.Quit()
    
except CZEx as ex:
    print(str(ex))

print("Done")