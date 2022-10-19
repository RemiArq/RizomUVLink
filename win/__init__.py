try:
	import win.rizomuvlink_python36 as rizomuvlink
except:
	try:
		import win.rizomuvlink_python37 as rizomuvlink
	except:
		try:
			import win.rizomuvlink_python38 as rizomuvlink
		except:
			try:
				import win.rizomuvlink_python39 as rizomuvlink
			except:
				try:
					import win.rizomuvlink_python310 as rizomuvlink
				except:
					raise "rizomuvlink version for the current python version not found"

