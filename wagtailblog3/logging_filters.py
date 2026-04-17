import logging


class ModuleFilter(logging.Filter):
	"""按模块过滤日志"""
	
	def __init__(self, modules=None):
		super().__init__()
		self.modules = modules or []
	
	def filter(self, record):
		# 如果modules为空，接受所有日志
		if not self.modules:
			return True
		
		# 检查记录的模块名是否在允许列表中
		for module in self.modules:
			if record.name.startswith(module):
				return True
		
		return False