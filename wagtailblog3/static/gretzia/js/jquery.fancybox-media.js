/*!
 * ========================================================================
 * Media Helper for fancyBox - fancyBox 媒体助手插件
 * 版本: 1.0.6 (日期: 2013年6月14日)
 * 依赖: fancyBox v2.0 或更高版本
 *
 * 用途:
 * 这个文件是 FancyBox 的一个辅助插件，专门用于增强 FancyBox 对各种在线媒体内容的处理能力。
 * 当你需要在 FancyBox 中展示来自 YouTube, Vimeo, Dailymotion 等视频网站的内容，
 * 或者 Google Maps, Instagram图片等媒体时，此助手可以自动识别链接格式，
 * 并将其转换为适合在 FancyBox iframe 或其他方式中嵌入的 URL 和类型。
 *
 * 主要功能:
 * - 自动识别多种媒体平台的 URL 格式。
 * - 支持的平台包括 (但不限于):
 * - YouTube (包括不同的 URL 格式，如 youtu.be, embed, watch)
 * - Vimeo
 * - Metacafe
 * - Dailymotion
 * - Twitvid
 * - Twitpic
 * - Instagram
 * - Google Maps
 * - 为识别出的媒体类型自动设置 FancyBox 的 `type` (通常是 'iframe' 或 'image')。
 * - 允许为每个媒体平台自定义 URL 参数 (例如，YouTube 的 autoplay, autohide 等)。
 * - 简化了在 FancyBox 中集成第三方媒体内容的流程。
 *
 * 如何使用:
 * 在初始化 FancyBox 时，通过 `helpers` 选项来启用和配置此媒体助手。
 * 例如:
 * $(".fancybox").fancybox({
 * helpers : {
 * media: true // 启用媒体助手，使用默认配置
 * }
 * });
 *
 * 或者自定义参数:
 * $(".fancybox").fancybox({
 * helpers : {
 * media: {
 * youtube : { // 针对 YouTube 的特定参数
 * params : {
 * autoplay : 0
 * }
 * },
 * vimeo : { // 针对 Vimeo 的特定参数
 * params : {
 * hd : 0
 * }
 * }
 * // ... 其他媒体平台的配置
 * }
 * }
 * });
 *
 * 注意:
 * 这个插件依赖于 FancyBox 主插件 (jquery.fancybox.js 或 jquery.fancybox.pack.js) 必须先被加载。
 * 它通过匹配链接的正则表达式来识别媒体来源，并据此调整 FancyBox 加载内容的方式。
 * ========================================================================
 */
/*!
 * Media helper for fancyBox
 * version: 1.0.6 (Fri, 14 Jun 2013)
 * @requires fancyBox v2.0 or later
 *
 * Usage:
 * $(".fancybox").fancybox({
 * helpers : {
 * media: true
 * }
 * });
 *
 * Set custom URL parameters:
 * $(".fancybox").fancybox({
 * helpers : {
 * media: {
 * youtube : {
 * params : {
 * autoplay : 0
 * }
 * }
 * }
 * }
 * });
 *
 * Or:
 * $(".fancybox").fancybox({,
 * helpers : {
 * media: true
 * },
 * youtube : {
 * autoplay: 0
 * }
 * });
 *
 * Supports:
 *
 * Youtube
 * http://www.youtube.com/watch?v=opj24KnzrWo
 * http://www.youtube.com/embed/opj24KnzrWo
 * http://youtu.be/opj24KnzrWo
 *			http://www.youtube-nocookie.com/embed/opj24KnzrWo
 * Vimeo
 * http://vimeo.com/40648169
 * http://vimeo.com/channels/staffpicks/38843628
 * http://vimeo.com/groups/surrealism/videos/36516384
 * http://player.vimeo.com/video/45074303
 * Metacafe
 * http://www.metacafe.com/watch/7635964/dr_seuss_the_lorax_movie_trailer/
 * http://www.metacafe.com/watch/7635964/
 * Dailymotion
 * http://www.dailymotion.com/video/xoytqh_dr-seuss_the_lorax_premiere_people
 * Twitvid
 * http://twitvid.com/QY7MD
 * Twitpic
 * http://twitpic.com/7p93st
 * Instagram
 * http://instagr.am/p/IejkuUGxQn/
 * http://instagram.com/p/IejkuUGxQn/
 * Google maps
 * http://maps.google.com/maps?q=Eiffel+Tower,+Avenue+Gustave+Eiffel,+Paris,+France&t=h&z=17
 * http://maps.google.com/?ll=48.857995,2.294297&spn=0.007666,0.021136&t=m&z=16
 * http://maps.google.com/?ll=48.859463,2.292626&spn=0.000965,0.002642&t=m&z=19&layer=c&cbll=48.859524,2.292532&panoid=YJ0lq28OOy3VT2IqIuVY0g&cbp=12,151.58,,0,-15.56
 */

(function ($) {
	"use strict";

	//Shortcut for fancyBox object
	var F = $.fancybox,
		format = function( url, rez, params ) {
			params = params || '';

			if ( $.type( params ) === "object" ) {
				params = $.param(params, true);
			}

			$.each(rez, function(key, value) {
				url = url.replace( '$' + key, value || '' );
			});

			if (params.length) {
				url += ( url.indexOf('?') > 0 ? '&' : '?' ) + params;
			}

			return url;
		};

	//Add helper object
	F.helpers.media = {
		defaults : {
			youtube : {
				matcher : /(youtube\.com|youtu\.be|youtube-nocookie\.com)\/(watch\?v=|v\/|u\/|embed\/?)?(videoseries\?list=(.*)|[\w-]{11}|\?listType=(.*)&list=(.*)).*/i,
				params  : {
					autoplay    : 1,
					autohide    : 1,
					fs          : 1,
					rel         : 0,
					hd          : 1,
					wmode       : 'opaque',
					enablejsapi : 1
				},
				type : 'iframe',
				url  : '//www.youtube.com/embed/$3'
			},
			vimeo : {
				matcher : /(?:vimeo(?:pro)?.com)\/(?:[^\d]+)?(\d+)(?:.*)/,
				params  : {
					autoplay      : 1,
					hd            : 1,
					show_title    : 1,
					show_byline   : 1,
					show_portrait : 0,
					fullscreen    : 1
				},
				type : 'iframe',
				url  : '//player.vimeo.com/video/$1'
			},
			metacafe : {
				matcher : /metacafe.com\/(?:watch|fplayer)\/([\w\-]{1,10})/,
				params  : {
					autoPlay : 'yes'
				},
				type : 'swf',
				url  : function( rez, params, obj ) {
					obj.swf.flashVars = 'playerVars=' + $.param( params, true );

					return '//www.metacafe.com/fplayer/' + rez[1] + '/.swf';
				}
			},
			dailymotion : {
				matcher : /dailymotion.com\/video\/(.*)\/?(.*)/,
				params  : {
					additionalInfos : 0,
					autoStart : 1
				},
				type : 'swf',
				url  : '//www.dailymotion.com/swf/video/$1'
			},
			twitvid : {
				matcher : /twitvid\.com\/([a-zA-Z0-9_\-\?\=]+)/i,
				params  : {
					autoplay : 0
				},
				type : 'iframe',
				url  : '//www.twitvid.com/embed.php?guid=$1'
			},
			twitpic : {
				matcher : /twitpic\.com\/(?!(?:place|photos|events)\/)([a-zA-Z0-9\?\=\-]+)/i,
				type : 'image',
				url  : '//twitpic.com/show/full/$1/'
			},
			instagram : {
				matcher : /(instagr\.am|instagram\.com)\/p\/([a-zA-Z0-9_\-]+)\/?/i,
				type : 'image',
				url  : '//$1/p/$2/media/?size=l'
			},
			google_maps : {
				matcher : /maps\.google\.([a-z]{2,3}(\.[a-z]{2})?)\/(\?ll=|maps\?)(.*)/i,
				type : 'iframe',
				url  : function( rez ) {
					return '//maps.google.' + rez[1] + '/' + rez[3] + '' + rez[4] + '&output=' + (rez[4].indexOf('layer=c') > 0 ? 'svembed' : 'embed');
				}
			}
		},

		beforeLoad : function(opts, obj) {
			var url   = obj.href || '',
				type  = false,
				what,
				item,
				rez,
				params;

			for (what in opts) {
				if (opts.hasOwnProperty(what)) {
					item = opts[ what ];
					rez  = url.match( item.matcher );

					if (rez) {
						type   = item.type;
						params = $.extend(true, {}, item.params, obj[ what ] || ($.isPlainObject(opts[ what ]) ? opts[ what ].params : null));

						url = $.type( item.url ) === "function" ? item.url.call( this, rez, params, obj ) : format( item.url, rez, params );

						break;
					}
				}
			}

			if (type) {
				obj.href = url;
				obj.type = type;

				obj.autoHeight = false;
			}
		}
	};

}(jQuery));