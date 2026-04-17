/*
* ========================================================================
* Gretzia 模板 - 主要自定义 JavaScript 文件 (Main Custom JavaScript File)
*
* - **用途 (Purpose)**:
* 这是 Gretzia 模板最主要的自定义 JavaScript 文件。
* 它通常包含了初始化各种 jQuery 插件的代码 (例如设置 Owl Carousel 的参数、激活 FancyBox、启动 WOW.js 等)，
* 以及模板特定的其他交互逻辑 (例如导航菜单的响应式行为、平滑滚动、返回顶部按钮、预加载动画等)。
*
* - **重要性 (Importance)**:
* 这是模板的核心功能脚本。理解这个文件对于确保模板所有交互功能正常工作至关重要。
* 如果之后某些交互不符合你的预期，或者你需要添加新的交互功能，你很可能会需要修改这个文件。
*
* - **依赖 (Dependencies)**:
* 通常依赖 jQuery 库。同时，它也依赖于页面中已加载的其他 jQuery 插件 (如 Owl Carousel, FancyBox, Isotope, WOW.js, jQuery Validate 等)。
* 这个脚本文件应在所有 jQuery 库和相关插件文件都加载完毕之后再加载执行。
*
* - **主要功能模块与函数 (Main Functional Modules & Functions)**:
*
* 1.  **预加载处理 (Preloader Handling)**:
* - `handlePreloader()`: 管理页面加载时的预加载动画效果，在页面加载完成后平滑隐藏预加载指示。
*
* 2.  **头部样式与滚动置顶 (Header Style & Scroll to Top)**:
* - `headerStyle()`: 根据页面滚动位置动态调整主导航头部的样式（例如添加 `fixed-header` 类使其固定在顶部），
* 并控制“返回顶部”按钮的淡入淡出。
* - 在页面加载时和滚动时都会调用 `headerStyle()`。
*
* 3.  **主导航菜单 (Main Menu / Navbar)**:
* - 响应式导航切换: 为 `.navbar-toggle` 按钮绑定点击事件，用于在小屏幕设备上切换导航菜单 (`.navbar-collapse`) 的显示/隐藏。
* - 下拉子菜单处理:
* - 为包含子菜单的 `li.dropdown` 元素动态添加下拉按钮 (`.dropdown-btn`)。
* - 为下拉按钮绑定点击事件，实现子菜单的滑动显示/隐藏 (`slideToggle`)。
* - 阻止了包含子菜单的一级链接的默认跳转行为，使其主要作为下拉触发器。
* - Mega Menu 样式类添加: 为 Mega Menu 中的特定 `ul` 元素添加 `first-ul` 和 `last-ul` 类，可能用于特定样式调整。
*
* 4.  **侧边栏与信息面板 (Sidebars & Info Panels)**:
* - `xs-sidebar-group`: 为其关闭按钮 (`.close-button`) 添加点击事件，移除 `isActive` 类以隐藏该侧边栏组。
* - `about-widget` / `about-sidebar`:
* - 点击 `.about-widget` 时为 `.about-sidebar` 添加 `active` 类以显示关于侧边栏。
* - 为关于侧边栏的关闭按钮 (`.close-button`) 和遮罩层 (`.gradient-layer`) 添加点击事件，移除 `active` 类以隐藏侧边栏。
*
* 5.  **隐藏式侧边栏菜单 (Hidden Bar Menu)**:
* - `hiddenBarMenuConfig()`: 配置隐藏式侧边栏菜单的行为。
* - 查找 `.hidden-bar .side-menu` 中的下拉菜单，并为其父级 `a` 标签绑定点击事件，
* 以切换子菜单的滑动显示/隐藏，并切换 `open` 类。
*
* 6.  **面板切换 (Panels Toggle - 例如购物车页面)**:
* - 为 `.toggle-panel` 元素绑定点击事件，切换自身的 `active-panel` 类，并淡入淡出其后紧邻的 `.toggle-content` 元素。
*
* 7.  **移动端菜单 (Mobile Menu)**:
* - 内容复制: 将主导航菜单 (`.main-header .nav-outer .main-menu`) 的 HTML 内容复制到移动端菜单结构 (`.mobile-menu .menu-box .menu-outer`) 和粘性头部 (`.sticky-header .main-menu`) 中。
* - 下拉子菜单处理 (Mobile):
* - 为移动菜单中的下拉按钮 (`.dropdown-btn`) 绑定点击事件，处理一级和多级子菜单的展开与折叠。
* - 包含对普通下拉菜单 `ul` 和 `div.mega-menu` 类型子菜单的分别处理逻辑。
* - 移动菜单显隐控制:
* - 为 `.mobile-nav-toggler` 按钮绑定点击事件，为 `body` 添加 `mobile-menu-visible` 类以显示移动菜单。
* - 为移动菜单的背景遮罩 (`.menu-backdrop`) 和关闭按钮 (`.close-btn`) 绑定点击事件，移除 `mobile-menu-visible` 类以隐藏移动菜单，并重置菜单展开状态。
* - 支持按 `ESC`键关闭移动菜单。
*
* 8.  **砌墙式布局 (Masonry Layout - Isotope Plugin)**:
* - `enableMasonry()`: 初始化 Isotope 插件，对 `.masonry-items-container` 中的 `.masonry-item` 应用砌墙式布局。
* - 监听窗口大小变化 (`resize`) 事件，重新应用 Isotope 布局以保持响应式。
*
* 9.  **轮播图/幻灯片 (Carousels - Owl Carousel Plugin)**:
* - 单项轮播 (`.single-item-carousel`): 初始化 Owl Carousel，配置为循环、自动播放、导航按钮等。
* - 三项轮播 (`.three-item-carousel`): 初始化 Owl Carousel，配置为循环、边距、自动播放、导航按钮，并为不同屏幕尺寸设置不同的显示项数。
*
* 10. **图片灯箱 (Lightbox - FancyBox Plugin)**:
* - 为类名为 `.lightbox-image` 的元素初始化 FancyBox，实现点击图片放大显示的效果，并配置了打开/关闭的淡入淡出效果。
*
* 11. **联系表单验证 (Contact Form Validation - jQuery Validate Plugin)**:
* - 为 ID 为 `#contact-form` 的表单初始化 jQuery Validation 插件，定义了用户名 (username)、邮箱 (email) 和消息 (message) 字段的验证规则 (如必填、邮箱格式)。
*
* 12. **平滑滚动到目标位置 (Scroll to Target)**:
* - 为类名为 `.scroll-to-target` 的元素绑定点击事件。
* - 点击时，获取其 `data-target` 属性值（目标元素的ID或选择器），并使用 jQuery 的 `animate` 方法平滑滚动页面到目标元素的位置。
*
* 13. **元素滚动入场动画 (Elements Animation - WOW.js Plugin)**:
* - 初始化 WOW.js 插件，配置其基本参数（如 `boxClass`, `animateClass`, `offset`, `mobile`, `live`）。
* - 当带有 `wow` 类的元素滚动进入可视区域时，会触发 `animate.css` 中定义的动画。
*
* - **事件监听 (Event Listeners)**:
* - 窗口滚动事件 (`$(window).on('scroll', ...)`):
* - 调用 `headerStyle()` 更新头部样式和返回顶部按钮状态。
* - 窗口加载完成事件 (`$(window).on('load', ...)`):
* - 调用 `handlePreloader()` 隐藏预加载动画。
* - 调用 `enableMasonry()` 初始化砌墙式布局。
*
* ========================================================================
*/

(function($) {
	
	"use strict";
	function handlePreloader() {
		if($('.preloader').length){
			$('.preloader').delay(200).fadeOut(500);
		}
	}
	
	//更新标题样式并滚动到顶部
	function headerStyle() {
		if($('.main-header').length){
			var windowpos = $(window).scrollTop();
			var siteHeader = $('.main-header');
			var scrollLink = $('.scroll-to-top');
			if (windowpos >= 250) {
				siteHeader.addClass('fixed-header');
				scrollLink.fadeIn(300);
			} else {
				siteHeader.removeClass('fixed-header');
				scrollLink.fadeOut(300);
			}
		}
	}
	
	headerStyle();
	
	if($('.main-menu').length){
		$('.navbar-toggle').click(function() {
		  $( ".navbar-collapse" ).fadeToggle( "18000" );
		});
	}
	
	//Submenu Dropdown Toggle
	if($('.main-header li.dropdown ul').length){

		// ===================== 新的悬停逻辑 (New Hover Logic) =====================

		// 为所有带下拉菜单的列表项 <li> 添加悬停事件
		$('.main-header li.dropdown').on('mouseenter', function() {
			// 当鼠标进入时，找到它的直接子级 'ul' 并让其滑出显示
			// .stop(true, true) 是为了防止快速移入移出时动画效果堆积
			$(this).children('ul').stop(true, true).slideDown(300);

		}).on('mouseleave', function() {
			// 当鼠标离开时，找到它的直接子级 'ul' 并让其滑动收起
			$(this).children('ul').stop(true, true).slideUp(300);
		});


		// ===================== 旧的点击逻辑 (已被移除或注释) =====================
		/* // 我们不再需要为父菜单项动态添加下拉按钮
		$('.main-header li.dropdown').append('<div class="dropdown-btn"><span class="fa fa-angle-down"></span></div>');

		// 我们不再需要为这个按钮绑定点击事件
		$('.main-header li.dropdown .dropdown-btn').on('click', function() {
			$(this).prev('ul').slideToggle(500);
		});

		// 【关键】我们移除了阻止链接跳转的这部分代码！
		// 现在点击父菜单项将可以正常跳转。
		$('.navigation li.dropdown > a').on('click', function(e) {
			// e.preventDefault();  <-- This line is now removed!
		});

		$('.main-header .navigation li.dropdown > a,.hidden-bar .side-menu li.dropdown > a').on('click', function(e) {
			// e.preventDefault();  <-- This line is now removed!
		});
		*/
	   // ========================================================================


		$('.main-menu .navigation > li .mega-menu-bar > .column > ul').addClass('first-ul');
		$('.main-header .main-menu .navigation > li > ul').addClass('last-ul');

		$('.xs-sidebar-group .close-button').on('click', function(e) {
			$('.xs-sidebar-group.info-group').removeClass('isActive');
		});

		$('.about-widget').on('click', function(e) {
			$('.about-sidebar').addClass('active');
		});

		$('.about-sidebar .close-button').on('click', function(e) {
			$('.about-sidebar').removeClass('active');
		});
		
		$('.about-sidebar .gradient-layer').on('click', function(e) {
			$('.about-sidebar').removeClass('active');
		});
		
	}
	
	
	//Hidden Bar Menu Config
	function hiddenBarMenuConfig() {
		var menuWrap = $('.hidden-bar .side-menu');
		// hidding submenu
		menuWrap.find('.dropdown').children('ul').hide();
		// toggling child ul
		menuWrap.find('li.dropdown > a').each(function () {
			$(this).on('click', function (e) {
				e.preventDefault();
				$(this).parent('li.dropdown').children('ul').slideToggle();

				// adding class to item container
				$(this).parent().toggleClass('open');

				return false;

			});
		});
	}

	hiddenBarMenuConfig();


	//Panels Toggle (Shopping Cart Page)
	if ($('.toggle-panel').length) {
		var targetPanel = $('.toggle-content');

		//Show Panel
		$('.toggle-panel').on('click', function () {
			$(this).toggleClass('active-panel');
			$(this).next(targetPanel).fadeToggle(300);
		});
	}
	
	
	
	
	
	
	
	//Mobile Nav Hide Show
	if($('.mobile-menu').length){
		//$('.mobile-menu .menu-box').mCustomScrollbar();
		var mobileMenuContent = $('.main-header .nav-outer .main-menu').html();
		$('.mobile-menu .menu-box .menu-outer').append(mobileMenuContent);
		$('.sticky-header .main-menu').append(mobileMenuContent);
		


		//Hide / Show Submenu
		$('.mobile-menu .navigation > li.dropdown > .dropdown-btn').on('click', function(e) {
			console.log('btn clicked');
			e.preventDefault();
			var target = $(this).parent('li').children('ul');
			var target1 = $(this).parent('li').children('div.mega-menu');
			// console.log('target', $(target).is(':visible'));
			console.log('target1', $(target1).is(':visible'));
			
			if ($(target).is(':visible')){
				$(this).parent('li').removeClass('open');
				$(target).slideUp(500);
				$(this).parents('.navigation').children('li.dropdown').removeClass('open');
				$(this).parents('.navigation').children('li.dropdown > ul.last-ul').slideUp(500);
				return false;
			} else{
				$(this).parents('.navigation').children('li.dropdown').removeClass('open');
				$(this).parents('.navigation').children('li.dropdown').children('ul.last-ul').slideUp(500);
				$(this).parent('li').toggleClass('open');
				$(this).parent('li').children('ul.last-ul').slideToggle(500);
			}
			if ($(target1).is(':visible')) {
				console.log('Visible');
				$(this).parent('li').removeClass('open');
				$(target1).slideUp(500);
				$(this).parents('.navigation').children('li.dropdown').removeClass('open');
				$(this).parents('.navigation').children('li.dropdown > .mega-menu').slideUp(500);
				// return false;
			} else {
				console.log('Not Visible');
				$(this).parents('.navigation').children('li.dropdown').removeClass('open');
				$(this).parents('.navigation').children('li.dropdown').children('.mega-menu').slideUp(500);
				$('.first-ul').css('display', 'block');
				$(this).parent('li').toggleClass('open');
				$(this).parent('li').children('.mega-menu').slideToggle(500);
			}
		});


		//3rd Level Nav
		$('.mobile-menu .navigation > li.dropdown > ul  > li.dropdown > .dropdown-btn').on('click', function(e) {
			e.preventDefault();
			var targetInner = $(this).parent('li').children('ul');
			
			if ($(targetInner).is(':visible')){
				$(this).parent('li').removeClass('open');
				$(targetInner).slideUp(500);
				$(this).parents('.navigation > ul').find('li.dropdown').removeClass('open');
				$(this).parents('.navigation > ul').find('li.dropdown > ul').slideUp(500);
				return false;
			}else{
				$(this).parents('.navigation > ul').find('li.dropdown').removeClass('open');
				$(this).parents('.navigation > ul').find('li.dropdown > ul').slideUp(500);
				$(this).parent('li').toggleClass('open');
				$(this).parent('li').children('ul').slideToggle(500);
			}
		});

		//Menu Toggle Btn
		$('.mobile-nav-toggler').on('click', function() {
			$('body').addClass('mobile-menu-visible');

		});



		//Menu Toggle Btn
		$('.mobile-menu .menu-backdrop,.mobile-menu .close-btn').on('click', function() {
			$('body').removeClass('mobile-menu-visible');
			$('.mobile-menu .navigation > li').removeClass('open');
			$('.mobile-menu .navigation li ul').slideUp(0);
		});

		$(document).keydown(function(e){
	        if(e.keyCode == 27) {
				$('body').removeClass('mobile-menu-visible');
			$('.mobile-menu .navigation > li').removeClass('open');
			$('.mobile-menu .navigation li ul').slideUp(0);
        	}
	    });
		
	}
	
	
	
	
	
	
	
	
	
	
	//Masonary
	function enableMasonry() {
		if($('.masonry-items-container').length){
	
			var winDow = $(window);
			// Needed variables
			var $container=$('.masonry-items-container');
	
			$container.isotope({
				itemSelector: '.masonry-item',
				 masonry: {
					columnWidth : 0
				 },
				animationOptions:{
					duration:500,
					easing:'linear'
				}
			});
	
			winDow.bind('resize', function(){

				$container.isotope({ 
					itemSelector: '.masonry-item',
					animationOptions: {
						duration: 500,
						easing	: 'linear',
						queue	: false
					}
				});
			});
		}
	}
	
	enableMasonry();
	
	
	//Single Item Carousel
	if ($('.single-item-carousel').length) {
		$('.single-item-carousel').owlCarousel({
			loop:true,
			margin:0,
			nav:true,
			smartSpeed: 700,
			autoplay: 4000,
			navText: [ '<span class="fa fa-angle-left"></span>', '<span class="fa fa-angle-right"></span>' ],
			responsive:{
				0:{
					items:1
				},
				480:{
					items:1
				},
				600:{
					items:1
				},
				800:{
					items:1
				},
				1024:{
					items:1
				}
			}
		});    		
	}
	
	
	//Three Item Carousel
	if ($('.three-item-carousel').length) {
		$('.three-item-carousel').owlCarousel({
			loop:true,
			margin:6,
			nav:true,
			smartSpeed: 700,
			autoplay: 4000,
			navText: [ '<span class="fa fa-long-arrow-left"></span>', '<span class="fa fa-long-arrow-right"></span>' ],
			responsive:{
				0:{
					items:1
				},
				480:{
					items:1
				},
				600:{
					items:2
				},
				800:{
					items:3
				},
				1024:{
					items:3
				},
				1400:{
					items:3
				}
			}
		});    		
	}
	
	
	//LightBox / Fancybox
	if($('.lightbox-image').length) {
		$('.lightbox-image').fancybox({
			openEffect  : 'fade',
			closeEffect : 'fade',
			helpers : {
				media : {}
			}
		});
	}
	
	
	//Contact Form Validation
	if($('#contact-form').length){
		$('#contact-form').validate({
			rules: {
				username: {
					required: true
				},
				email: {
					required: true,
					email: true
				},
				message: {
					required: true
				}
			}
		});
	}
	
	
	// Scroll to a Specific Div
	if($('.scroll-to-target').length){
		$(".scroll-to-target").on('click', function() {
			var target = $(this).attr('data-target');
		   // animate
		   $('html, body').animate({
			   scrollTop: $(target).offset().top
			 }, 1000);
		});
	}
	
	
	// Elements Animation
	if($('.wow').length){
		var wow = new WOW(
		  {
			boxClass:     'wow',      // animated element css class (default is wow)
			animateClass: 'animated', // animation css class (default is animated)
			offset:       0,          // distance to the element when triggering the animation (default is 0)
			mobile:       false,       // trigger animations on mobile devices (default is true)
			live:         true       // act on asynchronously loaded content (default is true)
		  }
		);
		wow.init();
	}

/* ==========================================================================
   When document is Scrollig, do
   ========================================================================== */
	
	$(window).on('scroll', function() {
		headerStyle();
	});
	
/* ==========================================================================
   When document is loaded, do
   ========================================================================== */
	
	$(window).on('load', function() {
		handlePreloader();
		enableMasonry();
	});	

})(window.jQuery);