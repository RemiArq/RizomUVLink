#
#https://stackoverflow.com/questions/458550/standard-way-to-embed-version-into-python-package

from RizomUVLinkBase import CRizomUVLinkBase
from RizomUVLinkBase import CZEx

class CRizomUVLink(CRizomUVLinkBase):
       def FakeFunction(self, params):
           pass
    
link = CRizomUVLink()

link.BindUrl()

params = {
    "path": "path/to/file.fbx",
    "XYZ": True
}
try:
    link.Execute("Load", params)
except CZEx as ex:
    print(str(ex))
    
print("Done")