#!/user/bin/env python3
# -*- coding: utf-8 -*-


"""
第三方文本编辑器的配置库,
包含wagtailcodeblock、wagtail-markdown、wagtailmedia 等第三方集成配置
"""

# ==========================================================
# Wagtail Code Block 配置
# ==========================================================
# Wagtail Code Block主题
WAGTAIL_CODE_BLOCK_THEME = 'okaidia'
# 启用行号（默认就是True，这里显式写出来方便理解）
WAGTAIL_CODE_BLOCK_LINE_NUMBERS = True
# 启用“复制到剪贴板”按钮（默认也是True）
WAGTAIL_CODE_BLOCK_COPY_TO_CLIPBOARD = True
# Wagtail Code Block配置
WAGTAIL_CODE_BLOCK_LANGUAGES = (
    ('abap', 'ABAP'),
    ('abnf', 'Augmented Backus–Naur form'),
    ('actionscript', 'ActionScript'),
    ('ada', 'Ada'),
    ('antlr4', 'ANTLR4'),
    ('apacheconf', 'Apache Configuration'),
    ('apl', 'APL'),
    ('applescript', 'AppleScript'),
    ('aql', 'AQL'),
    ('arduino', 'Arduino'),
    ('arff', 'ARFF'),
    ('asciidoc', 'AsciiDoc'),
    ('asm6502', '6502 Assembly'),
    ('aspnet', 'ASP.NET (C#)'),
    ('autohotkey', 'AutoHotkey'),
    ('autoit', 'AutoIt'),
    ('bash', 'Bash + Shell'),
    ('basic', 'BASIC'),
    ('batch', 'Batch'),
    ('bison', 'Bison'),
    ('bnf', 'Backus–Naur form + Routing Backus–Naur form'),
    ('brainfuck', 'Brainfuck'),
    ('bro', 'Bro'),
    ('c', 'C'),
    ('clike', 'C-like'),
    ('cmake', 'CMake'),
    ('csharp', 'C#'),
    ('cpp', 'C++'),
    ('cil', 'CIL'),
    ('coffeescript', 'CoffeeScript'),
    ('clojure', 'Clojure'),
    ('crystal', 'Crystal'),
    ('csp', 'Content-Security-Policy'),
    ('css', 'CSS'),
    ('css-extras', 'CSS Extras'),
    ('d', 'D'),
    ('dart', 'Dart'),
    ('diff', 'Diff'),
    ('django', 'Django/Jinja2'),
    ('dns-zone-file', 'DNS Zone File'),
    ('docker', 'Docker'),
    ('ebnf', 'Extended Backus–Naur form'),
    ('eiffel', 'Eiffel'),
    ('ejs', 'EJS'),
    ('elixir', 'Elixir'),
    ('elm', 'Elm'),
    ('erb', 'ERB'),
    ('erlang', 'Erlang'),
    ('etlua', 'Embedded LUA Templating'),
    ('fsharp', 'F#'),
    ('flow', 'Flow'),
    ('fortran', 'Fortran'),
    ('ftl', 'Freemarker Template Language'),
    ('gcode', 'G-code'),
    ('gdscript', 'GDScript'),
    ('gedcom', 'GEDCOM'),
    ('gherkin', 'Gherkin'),
    ('git', 'Git'),
    ('glsl', 'GLSL'),
    ('gml', 'GameMaker Language'),
    ('go', 'Go'),
    ('graphql', 'GraphQL'),
    ('groovy', 'Groovy'),
    ('haml', 'Haml'),
    ('handlebars', 'Handlebars'),
    ('haskell', 'Haskell'),
    ('haxe', 'Haxe'),
    ('hcl', 'HCL'),
    ('http', 'HTTP'),
    ('hpkp', 'HTTP Public-Key-Pins'),
    ('hsts', 'HTTP Strict-Transport-Security'),
    ('ichigojam', 'IchigoJam'),
    ('icon', 'Icon'),
    ('inform7', 'Inform 7'),
    ('ini', 'Ini'),
    ('io', 'Io'),
    ('j', 'J'),
    ('java', 'Java'),
    ('javadoc', 'JavaDoc'),
    ('javadoclike', 'JavaDoc-like'),
    ('javascript', 'JavaScript'),
    ('javastacktrace', 'Java stack trace'),
    ('jolie', 'Jolie'),
    ('jq', 'JQ'),
    ('jsdoc', 'JSDoc'),
    ('js-extras', 'JS Extras'),
    ('js-templates', 'JS Templates'),
    ('json', 'JSON'),
    ('jsonp', 'JSONP'),
    ('json5', 'JSON5'),
    ('julia', 'Julia'),
    ('keyman', 'Keyman'),
    ('kotlin', 'Kotlin'),
    ('latex', 'LaTeX'),
    ('less', 'Less'),
    ('lilypond', 'Lilypond'),
    ('liquid', 'Liquid'),
    ('lisp', 'Lisp'),
    ('livescript', 'LiveScript'),
    ('lolcode', 'LOLCODE'),
    ('lua', 'Lua'),
    ('makefile', 'Makefile'),
    ('markdown', 'Markdown'),
    ('markup', 'Markup + HTML + XML + SVG + MathML'),
    ('markup-templating', 'Markup templating'),
    ('matlab', 'MATLAB'),
    ('mel', 'MEL'),
    ('mizar', 'Mizar'),
    ('monkey', 'Monkey'),
    ('n1ql', 'N1QL'),
    ('n4js', 'N4JS'),
    ('nand2tetris-hdl', 'Nand To Tetris HDL'),
    ('nasm', 'NASM'),
    ('nginx', 'nginx'),
    ('nim', 'Nim'),
    ('nix', 'Nix'),
    ('nsis', 'NSIS'),
    ('objectivec', 'Objective-C'),
    ('ocaml', 'OCaml'),
    ('opencl', 'OpenCL'),
    ('oz', 'Oz'),
    ('parigp', 'PARI/GP'),
    ('parser', 'Parser'),
    ('pascal', 'Pascal + Object Pascal'),
    ('pascaligo', 'Pascaligo'),
    ('pcaxis', 'PC Axis'),
    ('perl', 'Perl'),
    ('php', 'PHP'),
    ('phpdoc', 'PHPDoc'),
    ('php-extras', 'PHP Extras'),
    ('plsql', 'PL/SQL'),
    ('powershell', 'PowerShell'),
    ('processing', 'Processing'),
    ('prolog', 'Prolog'),
    ('properties', '.properties'),
    ('protobuf', 'Protocol Buffers'),
    ('pug', 'Pug'),
    ('puppet', 'Puppet'),
    ('pure', 'Pure'),
    ('python', 'Python'),
    ('q', 'Q (kdb+ database)'),
    ('qore', 'Qore'),
    ('r', 'R'),
    ('jsx', 'React JSX'),
    ('tsx', 'React TSX'),
    ('renpy', 'Ren\'py'),
    ('reason', 'Reason'),
    ('regex', 'Regex'),
    ('rest', 'reST (reStructuredText)'),
    ('rip', 'Rip'),
    ('roboconf', 'Roboconf'),
    ('robot-framework', 'Robot Framework'),
    ('ruby', 'Ruby'),
    ('rust', 'Rust'),
    ('sas', 'SAS'),
    ('sass', 'Sass (Sass)'),
    ('scss', 'Sass (Scss)'),
    ('scala', 'Scala'),
    ('scheme', 'Scheme'),
    ('shell-session', 'Shell Session'),
    ('smalltalk', 'Smalltalk'),
    ('smarty', 'Smarty'),
    ('solidity', 'Solidity (Ethereum)'),
    ('sparql', 'SPARQL'),
    ('splunk-spl', 'Splunk SPL'),
    ('sqf', 'SQF: Status Quo Function (Arma 3)'),
    ('sql', 'SQL'),
    ('soy', 'Soy (Closure Template)'),
    ('stylus', 'Stylus'),
    ('swift', 'Swift'),
    ('tap', 'TAP'),
    ('tcl', 'Tcl'),
    ('textile', 'Textile'),
    ('toml', 'TOML'),
    ('tt2', 'Template Toolkit 2'),
    ('twig', 'Twig'),
    ('typescript', 'TypeScript'),
    ('t4-cs', 'T4 Text Templates (C#)'),
    ('t4-vb', 'T4 Text Templates (VB)'),
    ('t4-templating', 'T4 templating'),
    ('vala', 'Vala'),
    ('vbnet', 'VB.Net'),
    ('velocity', 'Velocity'),
    ('verilog', 'Verilog'),
    ('vhdl', 'VHDL'),
    ('vim', 'vim'),
    ('visual-basic', 'Visual Basic'),
    ('wasm', 'WebAssembly'),
    ('wiki', 'Wiki markup'),
    ('xeora', 'Xeora + XeoraCube'),
    ('xojo', 'Xojo (REALbasic)'),
    ('xquery', 'XQuery'),
    ('yaml', 'YAML'),
    ('zig', 'Zig'),
)


# ==========================================================
# Wagtail Markdown 配置
# ==========================================================
WAGTAILMARKDOWN = {
	"autodownload_fontawesome": False,
	
	# 扩展列表
	"extensions": [
		# 基础扩展
		'markdown.extensions.tables',
		'markdown.extensions.footnotes',
		'markdown.extensions.def_list',
		'markdown.extensions.attr_list',
		'markdown.extensions.abbr',
		'markdown.extensions.toc',
		'markdown.extensions.smarty',
		'markdown.extensions.nl2br',
		'markdown.extensions.sane_lists',
		
		# Pymdownx 扩展
		'pymdownx.arithmatex',
		'pymdownx.superfences',  # 🔥 关键：处理代码块和 Mermaid
		'pymdownx.highlight',  # 🔥 代码高亮
		'pymdownx.inlinehilite',  # 行内代码高亮
		'pymdownx.details',
		'pymdownx.tabbed',
		'pymdownx.tasklist',
		'pymdownx.mark',
		'pymdownx.tilde',
	],
	
	# 🔥 关键：扩展配置
	"extension_configs": {
		# 数学公式
		"pymdownx.arithmatex": {
			"generic": True
		},
		
		# 代码高亮配置
		"pymdownx.highlight": {
			"linenums": False,
			"guess_lang": False,
			"use_pygments": True,
			"pygments_style": "monokai",
		}
	},
	
	# 🔥 Bleach 清理配置 - 必须允许 pre 和 code
	"allowed_tags": [
		'div', 'span', 'p', 'a', 'img', 'pre', 'code', 'br', 'hr',
		'table', 'tr', 'th', 'td', 'thead', 'tbody', 'tfoot',
		'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del',
		'ul', 'ol', 'li', 'u', 'tt', 'sup', 'sub', 'dl', 'dd', 'dt',
		'caption', 'colgroup', 'ins', 'mark', 'blockquote', 'details', 'summary',
	],
	
	"allowed_styles": [
		'color', 'background-color', 'font-family', 'font-weight',
		'text-align', 'width', 'height', 'margin', 'padding',
		'font-size', 'border',
	],
	
	"allowed_attributes": {
		'*': ['class', 'style', 'id'],
		'a': ['href', 'title', 'target', 'rel'],
		'img': ['src', 'alt', 'title', 'width', 'height', 'loading'],
		'code': ['class', 'data-lang'],  # 🔥 允许 data-lang 属性
		'pre': ['class'],
		'div': ['class', 'id'],
		'span': ['class', 'id', 'style'],
		'table': ['class'],
	},
}


# ==========================================================
# Wagtail Media 配置
# ==========================================================
WAGTAILMEDIA = {
    "MEDIA_MODEL": "wagtailmedia.Media",  # 使用的媒体模型类
    "AUDIO_EXTENSIONS": [  # 允许上传的音频文件扩展名
        "aac", "aiff", "flac", "m4a", "m4b", "mp3", "ogg", "wav",
    ],
    "VIDEO_EXTENSIONS": [  # 允许上传的视频文件扩展名
        "avi", "h264", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "ogv", "webm",
    ],
}