/*
 * ========================================================================
 * Respond.js - min/max-width Media Query Polyfill (min/max-width 媒体查询腻子脚本)
 * 版本 (Version): 1.4.2 (通常版本，具体版本请参照实际下载来源，此代码本身未明确标出版本号，但功能与 1.4.x 系列一致)
 * 作者 (Author): Scott Jehl
 * 许可证 (License): MIT License (j.mp/respondjs)
 * 项目地址 (Project URL): https://github.com/scottjehl/Respond (官方)
 * ========================================================================
 *
 * - **用途 (Purpose)**:
 * Respond.js 是一个小型、专注的 JavaScript 脚本 (polyfill)，其主要目的是让不支持 CSS3 媒体查询
 * (特别是 min-width 和 max-width 特性) 的旧版浏览器（主要是 Internet Explorer 6、7、8）
 * 能够理解并应用这些媒体查询中定义的样式。这使得基于媒体查询的响应式网页设计
 * (Responsive Web Design) 能够在这些旧浏览器上获得一定程度的兼容性。
 *
 * - **重要性 (Importance)**:
 * 对于现代浏览器来说，此脚本不是必需的，因为它们原生支持 CSS3 媒体查询。
 * 如果你的目标用户群体中仍有大量使用 IE6-IE8 的用户，并且你需要项目在这些浏览器上实现响应式布局，
 * Respond.js 就可能非常有用。对于完全针对现代浏览器的项目，可以考虑不使用或移除此脚本。
 * 它通常与 html5shiv.js (HTML5 元素支持补丁) 一起用于兼容旧版 IE。
 *
 * - **依赖 (Dependencies)**:
 * 无显式 JavaScript 库依赖 (例如不依赖 jQuery)。它独立工作。
 *
 * - **主要功能 (Main Functions/Capabilities)**:
 * 1.  **媒体查询解析**: 脚本会通过 AJAX (异步 JavaScript 和 XML) 请求页面中链接的 CSS 文件。
 * 2.  **规则提取**: 解析获取到的 CSS 文件内容，查找 `@media` 规则块，特别是包含 `min-width` 和 `max-width` 条件的规则。
 * 3.  **条件判断**: 根据当前浏览器的视口宽度，判断哪些 `min-width` 和 `max-width` 条件被满足。
 * 4.  **样式动态应用**: 将满足条件的媒体查询块内的 CSS 规则提取出来，并动态地将这些规则添加到页面的 `<head>` 中的一个新的 `<style>` 标签内，从而使样式生效。
 * 5.  **窗口大小调整响应**: 监听浏览器窗口的 `resize` 事件。当窗口大小改变时，重新评估媒体查询条件并更新应用的样式。
 * 6.  **EM 单位转换**: 能够处理媒体查询条件中使用 `em` 单位的情况，将其转换为像素值进行比较。
 * 7.  **跨域限制**: 由于使用 AJAX 请求 CSS 文件，对于跨域的 CSS 文件，需要服务器配置正确的 CORS (Cross-Origin Resource Sharing) 头部，否则可能无法获取和解析。
 *
 * - **核心函数/方法与概念 (Core Functions/Methods & Concepts)**:
 * 1.  **`respond` 对象**:
 * - **说明**: 暴露在全局作用域 (通常是 `window.respond`) 的主要命名空间。
 * - `respond.update()`: 提供一个公共方法，用于在需要时手动触发媒体查询的重新评估和应用。在原生支持媒体查询的浏览器中，这是一个空函数。
 * - `respond.mediaQueriesSupported`: (布尔值) 标志，指示当前浏览器是否原生支持 `matchMedia` API。如果为 `true`，Respond.js 会提前退出，不执行任何操作。
 * 2.  **AJAX 相关**:
 * - `xmlHttp()`: 一个立即执行函数表达式 (IIFE)，返回一个 XMLHttpRequest 对象（或 IE 的 ActiveXObject）的工厂函数，用于创建 AJAX 请求。
 * - `ajax(url, callback)`: 封装的 AJAX GET 请求函数，用于异步获取指定 URL (CSS 文件) 的内容。
 * 3.  **CSS 解析与处理**:
 * - `respond.regex` (对象): 包含多个正则表达式，用于：
 * - 匹配 `@media` 规则块 (`media`)。
 * - 移除 CSS 注释 (`comments`) 和 `@keyframes` 规则 (`keyframes`) 以简化解析。
 * - 修正 CSS 中 `url()` 路径的相对性 (`urls`)。
 * - 查找媒体查询条件和规则内容 (`findStyles`)。
 * - 解析 `min-width` (`minw`) 和 `max-width` (`maxw`) 条件及其单位 (px 或 em)。
 * - `isUnsupportedMediaQuery(query)`: 检查媒体查询片段是否包含 Respond.js 不支持的条件 (除了 min/max-width/height)。
 * - `translate(styles, href, media)`:
 * - **说明**: 核心解析函数。接收 CSS 文本、原始 CSS 文件路径 (`href`) 和 `<link>` 标签的 `media` 属性值。
 * - 它会剥离注释和关键帧，然后使用正则表达式匹配所有 `@media` 块。
 * - 对每个媒体查询块，提取其媒体条件 (如 `screen and (min-width: 768px)`) 和内部的 CSS 规则。
 * - 将解析出的条件 (minw, maxw) 和规则索引存储到 `mediastyles` 数组中，规则本身存储到 `rules` 数组。
 * - 调用 `repUrls` 修正 CSS 规则中相对 URL 的路径。
 * - 最后调用 `applyMedia()` 来应用样式。
 * - `ripCSS()`:
 * - **说明**: 遍历文档中所有的 `<link rel="stylesheet">` 标签。
 * - 对每个符合条件的 CSS 文件，如果与当前页面同域，则将其 href 和 media属性加入 `requestQueue` 队列，等待通过 AJAX 获取内容。
 * - 如果是内联样式表或者已通过 `styleSheet.rawCssText` (特定浏览器) 可直接访问，则直接调用 `translate`。
 * - `makeRequests()`: 递归处理 `requestQueue` 队列，逐个发起 AJAX 请求获取 CSS 文件内容，并回调 `translate`。
 * 4.  **样式应用与更新**:
 * - `applyMedia(fromResize)`:
 * - **说明**: 核心的样式应用函数。在初始化、CSS加载完成或窗口大小调整时被调用。
 * - 获取当前视口宽度。
 * - 如果媒体查询条件中使用了 `em` 单位，则调用 `getEmValue()` 将其转换为像素。
 * - 遍历 `mediastyles` 中存储的所有已解析的媒体查询条件。
 * - 判断当前视口宽度是否满足各个媒体查询的 `min-width` 和 `max-width` 条件。
 * - 将所有满足条件的 CSS 规则收集起来，按媒体类型 (media type) 分组。
 * - 移除之前由 Respond.js 添加的 `<style>` 标签。
 * -为每个活动的媒体类型创建一个新的 `<style>` 标签，将收集到的 CSS 规则写入其中，并插入到文档头部。
 * - 包含一个针对 `resize` 事件的节流 (throttle) 机制，以避免过于频繁地执行。
 * - `getEmValue()`:
 * - **说明**: 计算当前浏览器中 `1em` 等于多少像素。通过创建一个临时的 `div` 元素并设置其 `font-size: 1em; width: 1em;` 然后获取其 `offsetWidth` 来实现。
 * 5.  **内部状态变量**:
 * - `requestQueue` (数组): 存储待通过 AJAX 请求的 CSS 文件信息。
 * - `mediastyles` (数组): 存储解析后的媒体查询对象，每个对象包含媒体类型、min/max 宽度条件和对应的规则索引。
 * - `rules` (数组): 存储从 CSS 文件中提取出来的原始 CSS 规则块文本。
 * - `appendedEls` (数组): 存储 Respond.js 动态添加到 `<head>` 中的 `<style>` 标签，以便后续移除。
 * - `parsedSheets` (对象): 记录已解析过的 CSS 文件 href，避免重复解析。
 * - `eminpx` (数字): 缓存 `1em` 对应的像素值。
 *
 * - **执行流程**:
 * 1. 检查浏览器是否原生支持媒体查询，如果支持则退出。
 * 2. 调用 `ripCSS()` 开始扫描和处理 CSS 文件。
 * 3. `ripCSS()` 将外部 CSS 文件加入请求队列 `requestQueue`。
 * 4. `makeRequests()` 逐个异步请求队列中的 CSS 文件。
 * 5. 获取到 CSS 内容后，调用 `translate()` 解析媒体查询和规则。
 * 6. `translate()` 调用 `applyMedia()` 根据当前视口宽度应用初始样式。
 * 7. 监听 `window.resize` 事件，当窗口大小改变时，再次调用 `applyMedia()` 更新样式。
 *
 * - **注意**:
 * Respond.js 的核心原理是通过 JavaScript 读取、解析 CSS 中的媒体查询，并在运行时根据浏览器窗口大小
 * 动态地将符合条件的 CSS 规则应用到页面上。它不直接“实现”媒体查询的全部功能，
 * 而是模拟了 `min-width` 和 `max-width` 这两种最常用的媒体查询条件的行为。
 *
 */
(function( w ){

	"use strict";

	//exposed namespace
	var respond = {};
	w.respond = respond;

	//define update even in native-mq-supporting browsers, to avoid errors
	respond.update = function(){}; // 在支持原生媒体查询的浏览器中定义一个空函数，以避免调用时出错

	//define ajax obj
	var requestQueue = [], // AJAX 请求队列
		xmlHttp = (function() { // XMLHttpRequest 对象工厂函数 (兼容旧版IE的ActiveXObject)
			var xmlhttpmethod = false;
			try {
				xmlhttpmethod = new w.XMLHttpRequest();
			}
			catch( e ){
				xmlhttpmethod = new w.ActiveXObject( "Microsoft.XMLHTTP" );
			}
			return function(){
				return xmlhttpmethod;
			};
		})(),

		//tweaked Ajax functions from Quirksmode
		// 封装的 AJAX GET 请求函数
		ajax = function( url, callback ) {
			var req = xmlHttp(); // 创建 XHR 对象
			if (!req){
				return;
			}
			req.open( "GET", url, true ); // 打开异步GET请求
			req.onreadystatechange = function () { // 状态改变回调
				if ( req.readyState !== 4 || req.status !== 200 && req.status !== 304 ){ // 请求未完成或状态码不正确
					return;
				}
				callback( req.responseText ); // 请求成功，调用回调函数并传入响应文本
			};
			if ( req.readyState === 4 ){ // 如果请求已在open之前完成 (不太可能发生)
				return;
			}
			req.send( null ); // 发送请求
		},
		// 检查媒体查询字符串中是否包含 Respond.js 不支持的条件 (除了min/max-width/height)
		isUnsupportedMediaQuery = function( query ) {
			return query.replace( respond.regex.minmaxwh, '' ).match( respond.regex.other );
		};

	//expose for testing (暴露给测试使用)
	respond.ajax = ajax;
	respond.queue = requestQueue;
	respond.unsupportedmq = isUnsupportedMediaQuery;
	respond.regex = { // 存储用于解析CSS的正则表达式
		media: /@media[^\{]+\{([^\{\}]*\{[^\}\{]*\})+/gi, // 匹配整个 @media ... {} 块
		keyframes: /@(?:\-(?:o|moz|webkit)\-)?keyframes[^\{]+\{(?:[^\{\}]*\{[^\}\{]*\})+[^\}]*\}/gi, // 匹配 @keyframes 块，用于移除
		comments: /\/\*[^*]*\*+([^/][^*]*\*+)*\//gi, // 匹配 CSS 注释 /* ... */，用于移除
		urls: /(url\()['"]?([^\/\)'"][^:\)'"]+)['"]?(\))/g, // 匹配 url(...) 中的路径，用于路径修正
		findStyles: /@media *([^\{]+)\{([\S\s]+?)$/, // 捕获媒体查询条件和对应的CSS规则块
		only: /(only\s+)?([a-zA-Z]+)\s?/, // 匹配 "only" 关键字和媒体类型 (如 screen, all)
		minw: /\(\s*min\-width\s*:\s*(\s*[0-9\.]+)(px|em)\s*\)/, // 匹配 (min-width: ...px/em)
		maxw: /\(\s*max\-width\s*:\s*(\s*[0-9\.]+)(px|em)\s*\)/, // 匹配 (max-width: ...px/em)
		minmaxwh: /\(\s*m(in|ax)\-(height|width)\s*:\s*(\s*[0-9\.]+)(px|em)\s*\)/gi, // 匹配所有 (min/max-width/height: ...px/em)
		other: /\([^\)]*\)/g // 匹配括号内的其他未知查询条件
	};

	//expose media query support flag for external use
	// 暴露一个标志，表明浏览器是否原生支持媒体查询
	respond.mediaQueriesSupported = w.matchMedia && w.matchMedia( "only all" ) !== null && w.matchMedia( "only all" ).matches;

	//if media queries are supported, exit here
	// 如果浏览器原生支持媒体查询，则 Respond.js 不需要执行任何操作，直接返回
	if( respond.mediaQueriesSupported ){
		return;
	}

	//define vars
	var doc = w.document, // 文档对象
		docElem = doc.documentElement, // HTML 根元素 <html>
		mediastyles = [], // 存储解析后的媒体查询对象 {media, rules, hasquery, minw, maxw}
		rules = [], // 存储从CSS文件中提取的原始CSS规则字符串
		appendedEls = [], // 存储由Respond.js动态添加到<head>的<style>标签
		parsedSheets = {}, // 记录已解析的CSS文件，避免重复解析，键为href，值为true
		resizeThrottle = 30, // 窗口大小调整事件的节流时间 (毫秒)
		head = doc.getElementsByTagName( "head" )[0] || docElem, // <head> 元素
		base = doc.getElementsByTagName( "base" )[0], // <base> 元素 (用于解析相对路径)
		links = head.getElementsByTagName( "link" ), // 页面中所有的 <link> 元素

		lastCall, // 上一次调用 applyMedia 的时间戳，用于节流
		resizeDefer, // setTimeout 的 ID，用于节流

		//cached container for 1em value, populated the first time it's needed
		eminpx, // 缓存1em对应的像素值

		// returns the value of 1em in pixels
		// 计算并返回1em等于多少像素
		getEmValue = function() {
			var ret,
				div = doc.createElement('div'), // 创建一个临时div
				body = doc.body,
				originalHTMLFontSize = docElem.style.fontSize, // 保存原始html和body的字体大小
				originalBodyFontSize = body && body.style.fontSize,
				fakeUsed = false; // 标记是否创建了临时的body

			div.style.cssText = "position:absolute;font-size:1em;width:1em"; // 设置临时div的样式

			if( !body ){ // 如果body不存在 (例如脚本在head中且body未解析完)
				body = fakeUsed = doc.createElement( "body" ); // 创建一个临时body
				body.style.background = "none";
			}

			// 在媒体查询中，1em 是浏览器默认字体大小的值
			// 重置 html 和 body 的字体大小以确保返回正确的值
			docElem.style.fontSize = "100%";
			body.style.fontSize = "100%";

			body.appendChild( div ); // 将临时div添加到body

			if( fakeUsed ){ // 如果创建了临时body，则将其插入到文档中
				docElem.insertBefore( body, docElem.firstChild );
			}

			ret = div.offsetWidth; // 获取临时div的宽度，即1em的像素值

			if( fakeUsed ){ // 如果创建了临时body，则移除
				docElem.removeChild( body );
			}
			else { // 否则只移除临时div
				body.removeChild( div );
			}

			// 恢复原始的字体大小值
			docElem.style.fontSize = originalHTMLFontSize;
			if( originalBodyFontSize ) {
				body.style.fontSize = originalBodyFontSize;
			}

			//更新 eminpx 缓存并返回
			ret = eminpx = parseFloat(ret);

			return ret;
		},

		//enable/disable styles
		// 根据当前视口宽度应用或移除媒体查询定义的样式
		applyMedia = function( fromResize ){ // fromResize 参数标记是否由resize事件触发
			var name = "clientWidth", // 用于获取视口宽度的属性名
				docElemProp = docElem[ name ],
				// 获取当前视口宽度 (兼容不同浏览器模式)
				currWidth = doc.compatMode === "CSS1Compat" && docElemProp || doc.body[ name ] || docElemProp,
				styleBlocks	= {}, // 存储当前激活的样式块，按媒体类型分组
				lastLink = links[ links.length-1 ], // 获取页面中最后一个<link>标签，新<style>将插在其后
				now = (new Date()).getTime(); // 当前时间戳

			//throttle resize calls (resize事件节流)
			if( fromResize && lastCall && now - lastCall < resizeThrottle ){
				w.clearTimeout( resizeDefer ); // 清除之前的延时
				resizeDefer = w.setTimeout( applyMedia, resizeThrottle ); // 设置新的延时
				return;
			}
			else {
				lastCall = now; // 更新上次调用的时间
			}

			// 遍历所有已解析的媒体查询样式
			for( var i in mediastyles ){
				if( mediastyles.hasOwnProperty( i ) ){
					var thisstyle = mediastyles[ i ],
						min = thisstyle.minw, // min-width 条件
						max = thisstyle.maxw, // max-width 条件
						minnull = min === null, // min-width 是否为null
						maxnull = max === null, // max-width 是否为null
						em = "em";

					// 如果min-width存在，将其转换为像素值 (如果是em单位)
					if( !!min ){
						min = parseFloat( min ) * ( min.indexOf( em ) > -1 ? ( eminpx || getEmValue() ) : 1 );
					}
					// 如果max-width存在，将其转换为像素值 (如果是em单位)
					if( !!max ){
						max = parseFloat( max ) * ( max.indexOf( em ) > -1 ? ( eminpx || getEmValue() ) : 1 );
					}

					// 判断当前视口宽度是否满足媒体查询条件
					// !thisstyle.hasquery: 如果没有具体的查询条件 (例如 `@media screen`)
					// (!minnull || !maxnull): 至少有一个min或max条件存在
					// (minnull || currWidth >= min): 如果没有min条件，或者当前宽度大于等于min
					// (maxnull || currWidth <= max): 如果没有max条件，或者当前宽度小于等于max
					if( !thisstyle.hasquery || ( !minnull || !maxnull ) && ( minnull || currWidth >= min ) && ( maxnull || currWidth <= max ) ){
						if( !styleBlocks[ thisstyle.media ] ){ // 按媒体类型 (如 'screen', 'all') 初始化数组
							styleBlocks[ thisstyle.media ] = [];
						}
						styleBlocks[ thisstyle.media ].push( rules[ thisstyle.rules ] ); // 将满足条件的CSS规则文本添加到对应媒体类型的数组中
					}
				}
			}

			//remove any existing respond style element(s)
			// 移除之前由Respond.js添加的所有<style>标签
			for( var j in appendedEls ){
				if( appendedEls.hasOwnProperty( j ) ){
					if( appendedEls[ j ] && appendedEls[ j ].parentNode === head ){
						head.removeChild( appendedEls[ j ] );
					}
				}
			}
			appendedEls.length = 0; // 清空已添加元素记录

			//inject active styles, grouped by media type
			// 将当前激活的样式块注入到页面中
			for( var k in styleBlocks ){
				if( styleBlocks.hasOwnProperty( k ) ){
					var ss = doc.createElement( "style" ), // 创建一个新的<style>标签
						css = styleBlocks[ k ].join( "\n" ); // 将同一媒体类型下的所有CSS规则合并

					ss.type = "text/css";
					ss.media = k; // 设置<style>标签的media属性

					// 将新<style>标签插入到最后一个<link>标签之后
					// 之前是批量追加，但会导致IE崩溃，所以改为逐个插入
					head.insertBefore( ss, lastLink.nextSibling );

					if ( ss.styleSheet ){ // IE方式写入样式
						ss.styleSheet.cssText = css;
					}
					else { // 标准方式写入样式
						ss.appendChild( doc.createTextNode( css ) );
					}

					//push to appendedEls to track for later removal
					// 记录这个新添加的<style>标签，以便下次更新时移除
					appendedEls.push( ss );
				}
			}
		},
		//find media blocks in css text, convert to style blocks
		// 解析CSS文本，提取媒体查询块和规则
		translate = function( styles, href, media ){ // styles: CSS文本, href: CSS文件路径, media: link标签的media属性
			// 移除注释和关键帧，然后匹配所有@media块
			var qs = styles.replace( respond.regex.comments, '' )
					.replace( respond.regex.keyframes, '' )
					.match( respond.regex.media ),
				ql = qs && qs.length || 0; // 匹配到的@media块数量

			//try to get CSS path (获取CSS文件所在的目录路径)
			href = href.substring( 0, href.lastIndexOf( "/" ) );

			// 用于修正CSS规则中相对URL路径的函数
			var repUrls = function( css ){
					return css.replace( respond.regex.urls, "$1" + href + "$2$3" );
				},
				useMedia = !ql && media; // 如果CSS内部没有@media块，但<link>标签有media属性，则使用该media属性

			//if path exists, tack on trailing slash (如果路径存在，添加末尾的斜杠)
			if( href.length ){ href += "/"; }

			//if no internal queries exist, but media attr does, use that
			// (如果CSS内部没有查询，但link标签有media属性，则使用该属性)
			// 注意: 当前缺乏对 <link media="A"> 且其CSS文件内部还有 `@media B` 的复杂情况支持。
			// 这种情况下，<link>的media属性目前会被忽略。
			if( useMedia ){
				ql = 1;
			}

			for( var i = 0; i < ql; i++ ){ // 遍历每个@media块
				var fullq, thisq, eachq, eql;

				if( useMedia ){ // 如果是使用<link>的media属性
					fullq = media; // 整个媒体查询字符串就是link的media属性值
					rules.push( repUrls( styles ) ); // 将整个CSS文件的内容（修正URL后）作为一个规则块存起来
				}
				else{ // 如果是解析CSS内部的@media块
					fullq = qs[ i ].match( respond.regex.findStyles ) && RegExp.$1; // 提取媒体查询条件部分
					rules.push( RegExp.$2 && repUrls( RegExp.$2 ) ); // 提取CSS规则部分，并修正URL
				}

				eachq = fullq.split( "," ); // 按逗号分割媒体查询条件 (例如 "screen and (min-width: 768px), print")
				eql = eachq.length;

				for( var j = 0; j < eql; j++ ){ // 遍历每个独立的查询条件
					thisq = eachq[ j ];

					// 如果包含 Respond.js 不支持的查询条件 (如 (orientation: landscape))，则跳过
					if( isUnsupportedMediaQuery( thisq ) ) {
						continue;
					}

					// 将解析后的媒体查询信息存入 mediastyles 数组
					mediastyles.push( {
						media : thisq.split( "(" )[ 0 ].match( respond.regex.only ) && RegExp.$2 || "all", // 提取媒体类型 (screen, all等)
						rules : rules.length - 1, // 指向 rules 数组中对应CSS规则的索引
						hasquery : thisq.indexOf("(") > -1, // 是否包含具体的查询条件 (如min/max-width)
						minw : thisq.match( respond.regex.minw ) && parseFloat( RegExp.$1 ) + ( RegExp.$2 || "" ), // 提取min-width值和单位
						maxw : thisq.match( respond.regex.maxw ) && parseFloat( RegExp.$1 ) + ( RegExp.$2 || "" ) // 提取max-width值和单位
					} );
				}
			}

			applyMedia(); // 解析完成后，立即应用一次样式
		},

		//recurse through request queue, get css text
		// 递归处理请求队列，获取CSS文本
		makeRequests = function(){
			if( requestQueue.length ){ // 如果队列不为空
				var thisRequest = requestQueue.shift(); // 取出队列中的第一个请求

				// 发起AJAX请求获取CSS文件内容
				ajax( thisRequest.href, function( styles ){
					translate( styles, thisRequest.href, thisRequest.media ); // 解析获取到的CSS内容
					parsedSheets[ thisRequest.href ] = true; // 标记此文件已解析

					// by wrapping recursive function call in setTimeout
					// we prevent "Stack overflow" error in IE7
					// 使用setTimeout递归调用，防止IE7栈溢出
					w.setTimeout(function(){ makeRequests(); },0);
				} );
			}
		},

		//loop stylesheets, send text content to translate
		// 遍历页面上的<link>标签，将CSS文件内容发送给translate函数处理
		ripCSS = function(){

			for( var i = 0; i < links.length; i++ ){ // 遍历所有link标签
				var sheet = links[ i ],
				href = sheet.href, // link的href属性
				media = sheet.media, // link的media属性
				isCSS = sheet.rel && sheet.rel.toLowerCase() === "stylesheet"; // 判断是否为CSS样式表

				//only links plz and prevent re-parsing (只处理CSS链接，并防止重复解析)
				if( !!href && isCSS && !parsedSheets[ href ] ){
					// selectivizr exposes css through the rawCssText expando
					// (一些特殊情况下，如使用了selectivizr库，可以直接获取CSS文本)
					if (sheet.styleSheet && sheet.styleSheet.rawCssText) {
						translate( sheet.styleSheet.rawCssText, href, media );
						parsedSheets[ href ] = true;
					} else {
						// 判断是否为同域CSS文件，或者是否有<base>标签 (影响相对路径解析)
						if( (!/^([a-zA-Z:]*\/\/)/.test( href ) && !base) || // 非绝对路径且无base标签 (即相对路径)
							href.replace( RegExp.$1, "" ).split( "/" )[0] === w.location.host ){ // 或主机名与当前页面一致 (同域)
							// IE7 doesn't handle urls that start with '//' for ajax request
							// manually add in the protocol
							// IE7处理以'//'开头的URL时有问题，手动添加协议
							if ( href.substring(0,2) === "//" ) { href = w.location.protocol + href; }
							requestQueue.push( { // 将同域CSS文件加入请求队列
								href: href,
								media: media
							} );
						}
						// 注意: 跨域的CSS文件默认不会被处理，除非服务器设置了CORS响应头
					}
				}
			}
			makeRequests(); // 开始处理请求队列
		};

	//translate CSS
	ripCSS(); // 初始化时执行一次CSS解析

	//expose update for re-running respond later on
	// 暴露 update 方法，以便之后可以手动重新运行Respond.js
	respond.update = ripCSS;

	//expose getEmValue
	// 暴露 getEmValue 方法
	respond.getEmValue = getEmValue;

	//adjust on resize
	// 窗口大小调整时调用 applyMedia
	function callMedia(){
		applyMedia( true );
	}

	// 绑定resize事件监听
	if( w.addEventListener ){
		w.addEventListener( "resize", callMedia, false );
	}
	else if( w.attachEvent ){
		w.attachEvent( "onresize", callMedia );
	}
})(this);