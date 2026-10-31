# -*- coding: utf-8 -*-
"""小天才 App 操作层：启动 / 登录检测 / 打开聊天 / 发送消息 / 读取最新消息。

UI 适配策略（基于真机实测，com.xtc.watch v 登录后界面）：
- 聊天窗口 ChatActivity 有两种输入模式：
    * 语音模式：底部是"按住说话"按钮（chat_record_button），没有 EditText；
    * 文字模式：点击底部左侧图标 iv_left_img_view（单次点击）切出
      EditText（et_chat_text_content）+ 发送按钮（tv_send_view，输入内容后才出现）。
- 聊天窗口判定：看聊天输入栏特征 id（et_chat_text_content / chat_record_button /
  chat_input），不再用 Activity 名猜（会误判弹窗/接收画面）。
- 弹窗处理：系统录音权限弹窗（允许前台使用）、小天才"警告"弹窗（点取消）。
- 读取消息：聊天窗口内取输入栏上方最靠下的一条文本气泡（剔除 UI 文案）；
  聊天列表取联系人行内的消息预览。
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from ..adb_controller import ADBController, AdbError

# 聊天窗口输入栏的特征 resource-id 末段（出现其一即认为在聊天页）
_CHAT_IDS = ("et_chat_text_content", "chat_record_button", "chat_input")
# 聊天页 resource-id 末段的**包含**特征：不同版本/机型 id 会有前缀后缀差异，
# 只认全等会漏判（用户反馈"明明在聊天页却以为我在主页"）。命中任一即视为聊天页。
_CHAT_ID_HINTS = ("chat_input", "chat_edit", "et_chat_text", "chat_text_content",
                  "chat_record", "record_button", "send_view", "btn_send", "tv_send",
                  "chat_msg_item", "chatting_input", "edit_chat")
# 聊天页 Activity 名的判断：含 chat，且不含这些"列表/首页"字样（避免把聊天列表当聊天页）
_ACTIVITY_CHAT_EXCLUDE = ("list", "main", "welcome", "login", "register", "signin",
                          "watchmsg", "msglist", "message")
# 消息列表行的联系人名 id（找不到联系人时用来给出"当前可见联系人"诊断）
_CONTACT_NAME_IDS = ("tv_chat_dialog_name", "tv_chat_dialog_title", "chat_dialog_name")
# 消息列表行的预览 id（用来判断"当前页面到底是不是消息列表"）
_CONTACT_PREVIEW_IDS = ("tv_chat_dialog_last_msg_content", "tv_chat_dialog_last_msg")
# 语音模式 -> 文字模式的切换按钮
_SWITCH_TO_TEXT_IDS = ("iv_left_img_view", "chat_record_button")
# 发送控件 id 线索（实测 tv_send_view，可点的是父 fl_chat_text_send）
_SEND_ID_HINTS = ("tv_send_view", "fl_chat_text_send", "btn_send", "send_view", "iv_send",
                  "chat_send", "send_button")
# 这些是"消息状态"图标（发送中/发送失败/重发），**不是**发送按钮，必须排除
_SEND_EXCLUDE_IDS = ("msg_item_state", "resend", "send_state", "send_fail", "msg_state")
# 系统录音权限弹窗按钮（不同 Android 版本 id 不同，全部尝试）
_PERMISSION_ALLOW_IDS = (
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.android.permissioncontroller:id/permission_allow_button",
    "com.android.permissioncontroller:id/permission_allow_one_time_button",
    "com.android.permissioncontroller:id/permission_allow_always_button",
    "com.android.packageinstaller:id/permission_allow_button",
)
# 小天才内部警告弹窗"取消"
_CANCEL_DIALOG_ID = "com.xtc.watch:id/btn_cancel"
# 网络提示条（会混进聊天文本，读取时排除）
_TIP_IDS = ("tv_weichat_uninstall_hint", "iv_tips_content")
# 图片消息（照片）在界面上的识别：气泡同样是 chat_msg_item_content，但 class 是 ImageView、
# text 为空、content-desc 形如 '童武洋发的消息,图片消息，双击查看'（图片还在下载时可能只有
# '童武洋发的消息'）。转成这个**固定文本**当消息内容/去重身份（不同照片靠时间标签区分）。
IMAGE_TEXT = "图片"
# 发送失败弹窗标题
_SEND_FAIL_TITLE = "消息发送"

# ---- 弹窗/遮挡处理用的短语表（均可被 config -> xiaotiancai.ui 覆盖） ----
# 关闭类按钮：resource-id 末段命中即点（这些 id 只出现在弹窗/浮层上）
_CLOSE_ID_TAILS = ("btn_close", "iv_close", "img_close", "tv_close", "close_btn",
                   "dialog_close", "btn_cancel", "iv_cancel", "btn_no", "tv_cancel")
# content-desc 命中且当前是弹窗窗口时才点
_CLOSE_DESCS = ("关闭", "取消", "close", "Cancel", "×")
# 更新/评价/公告类弹窗：优先点"稍后/以后再说"，避免误触下载
_UPDATE_WORDS = ("发现新版本", "版本更新", "立即更新", "马上更新", "升级", "去评分",
                 "给个好评", "评价一下", "公告", "活动", "福利", "签到有礼")
_SKIP_TEXTS = ("以后再说", "稍后再说", "暂不更新", "不更新", "稍后更新", "下次再说", "暂不升级",
               "忽略此版本", "先逛逛", "放弃", "不同意", "取消", "关闭")
# 系统无响应/崩溃弹窗
_ANR_WORDS = ("无响应", "没有响应", "已停止运行", "屡次停止运行", "反复停止")
_ANR_WAIT = ("等待", "等待响应", "继续等待")
# 网络类临时弹窗：点"重试/知道了"（点重试有冷却，避免死循环）
_NET_WORDS = ("网络异常", "网络连接失败", "连接失败", "网络不可用", "请检查网络",
              "服务器繁忙", "加载失败")
_NET_RETRY = ("重试", "再试一次", "重新加载", "刷新")
_NET_DISMISS = ("知道了", "确定", "好的", "取消", "关闭")
# 权限弹窗的文本按钮兜底
_PERMISSION_TEXTS = ("允许", "始终允许", "仅在使用中允许", "使用应用时允许", "同意", "确定")

# ---- 通用弹窗（自定义 Activity 弹窗 / Dialog / PopupWindow）----
# 弹窗类 Activity 的名字里常带这些词：小天才的"升级提醒"就是
# com.xtc.watch/com.xtc.widget.phone.popup.activity.CustomActivity14
# （它既不是 PopupWindow 也不是 Dialog，以前认不出来，所以永远不会被自动关掉）
_POPUP_ACTIVITY_HINTS = ("popup", "dialog", "alert")
# 弹窗标题/说明控件（聊天页、消息列表页不会有）
_POPUP_TITLE_TAILS = ("title", "tv_title", "dialog_title", "tv_dialog_title", "alert_title")
# 弹窗容器（自定义弹窗常用 ll_center / iv_banner 这类 id）
_POPUP_CONTAINER_TAILS = ("ll_center", "fl_center", "iv_banner", "dialog_root",
                          "dialog_container", "alert_root", "popup_root")
# 负向（安全）按钮：点它只是"不做这件事"，不会触发下载/安装/重启
_POPUP_NEG_TAILS = ("btn_left", "btn_cancel", "tv_cancel", "btn_no", "tv_no",
                    "btn_negative", "tv_negative", "btn_disagree", "btn_later", "tv_left")
# 正向/危险按钮：**绝不点**（立即安装/立即更新/确定安装 这类会真的动系统）
_POPUP_POS_TAILS = ("btn_right", "btn_sure", "btn_ok", "btn_confirm", "tv_right",
                    "btn_positive", "btn_install", "btn_update", "btn_restart")
# 危险按钮文案（命中且不含"不/稍后/取消"这类否定词时才禁点）
_POPUP_BLOCK_TEXTS = ("立即安装", "马上安装", "现在安装", "去安装", "确认安装", "下载安装",
                      "立即更新", "马上更新", "立即升级", "马上升级", "现在升级", "去升级",
                      "立即重启", "去评分", "给个好评", "同意并安装")
# 否定词：带这些词的按钮一律视为"安全的负向按钮"
_POPUP_SAFE_WORDS = ("不", "稍后", "以后", "下次", "暂", "取消", "关闭", "忽略",
                     "拒绝", "返回", "放弃")

# 登录页"正在进行"的文案：出现这些一律**不判失败**，继续等待
_DEFAULT_LOGIN_PROGRESS = ("登录中", "正在登录", "正在验证", "验证中", "正在提交", "提交中",
                           "请稍候", "请稍后", "正在加载", "加载中", "处理中", "请等待",
                           "登录中...", "正在登录...")
# 明确的账号/密码类错误（命中即判定登录失败）
_STRONG_LOGIN_ERRORS = ("密码错误", "密码不正确", "账号或密码错误", "账号不存在", "用户不存在",
                        "手机号未注册", "手机号不存在", "验证码错误", "验证码已失效",
                        "验证码不正确", "次数过多", "账号异常", "账号已被", "已被封禁",
                        "密码格式", "手机号格式")

_DEFAULT_JUNK = ["发送", "表情", "语音", "拍照", "更多", "已读", "撤回", "按住说话",
                 "试试和宝贝聊天吧", "试试将作业要求发送给宝贝吧"]
# 输入框为空时会显示"占位提示"（hint），dump 出来的 text 就是这段文案；
# 必须当成"空"，否则清残留/发送确认都会把它误当成用户输入（实机测试发现）。
_DEFAULT_INPUT_HINTS = ("发送文字", "发送消息", "输入内容", "说点什么", "输入消息", "发消息")

# 登录态三态（模块级常量，bridge 等模块直接引用，避免依赖具体实例）
LOGIN_LOGGED_IN = "logged_in"
LOGIN_NOT_LOGGED_IN = "not_logged_in"
LOGIN_UNKNOWN = "unknown"

# 登录页**常驻**文案：`risk_markers` 的命中若只是这些文案的一部分，就不算"触发安全验证"。
# 背景：随包模板曾把"验证码"写进 risk_markers 默认值，而登录页底部固定写着
# "短信验证码登录 / 获取验证码"（state.py 正是拿它们当登录页特征），于是用默认配置的
# 用户每次自动登录都在第一轮被判成风险界面，按 login_retry_after_risk（默认 900 秒）
# 挂起等待人工，而真正的密码错误反而看不见。
_RISK_FALSE_POSITIVES = ("短信验证码登录", "验证码登录", "获取验证码", "找回密码")



# 动态 __all__：让各子模块 `from .constants import *` 也能拿到下划线开头的内部常量
__all__ = [__name for __name in list(globals()) if not __name.startswith('__')]
