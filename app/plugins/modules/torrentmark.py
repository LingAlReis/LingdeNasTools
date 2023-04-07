import re
from datetime import datetime
from threading import Event

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from lxml import etree

from app.downloader import Downloader
from app.media.meta import MetaInfo
from app.message import Message
from app.plugins.modules._base import _IPluginModule
from app.utils import RequestUtils
from app.utils.types import DownloaderType
from config import Config


class TorrentMark(_IPluginModule):
    # 插件名称
    module_name = "种子标记"
    # 插件描述
    module_desc = "标记种子是否是PT。"
    # 插件图标
    module_icon = "tag.png"
    # 主题色
    module_color = "#4876b6"
    # 插件版本
    module_version = "1.0"
    # 插件作者
    module_author = "linyuan0213"
    # 作者主页
    author_url = "https://github.com/linyuan0213"
    # 插件配置项ID前缀
    module_config_prefix = "torrentmark_"
    # 加载顺序
    module_order = 10
    # 可使用的用户级别
    user_level = 1

    # 私有属性
    _scheduler = None
    downloader = None
    # 限速开关
    _enable = False
    _cron = None
    _onlyonce = False
    _downloaders = []
    _nolabels = None
    # 退出事件
    _event = Event()

    @staticmethod
    def get_fields():
        downloaders = {k: v for k, v in Downloader().get_downloader_conf_simple().items()
                       if v.get("type") in ["qbittorrent", "transmission"] and v.get("enabled")}
        return [
            # 同一板块
            {
                'type': 'div',
                'content': [
                    # 同一行
                    [
                        {
                            'title': '开启种子标记',
                            'required': "",
                            'tooltip': '开启后，自动监控下载器，对下载完成的任务根据执行周期标记。',
                            'type': 'switch',
                            'id': 'enable',
                        }
                    ],
                    [
                        {
                            'title': '执行周期',
                            'required': "required",
                            'tooltip': '标记任务执行的时间周期，支持5位cron表达式；应避免任务执行过于频繁',
                            'type': 'text',
                            'content': [
                                {
                                    'id': 'cron',
                                    'placeholder': '0 0 0 ? *',
                                }
                            ]
                        }
                    ]
                ]
            },
            {
                'type': 'details',
                'summary': '下载器',
                'tooltip': '只有选中的下载器才会执行标记',
                'content': [
                    # 同一行
                    [
                        {
                            'id': 'downloaders',
                            'type': 'form-selectgroup',
                            'content': downloaders
                        },
                    ]
                ]
            },
            {
                'type': 'div',
                'content': [
                    # 同一行
                    [
                        {
                            'title': '立即运行一次',
                            'required': "",
                            'tooltip': '打开后立即运行一次（点击此对话框的确定按钮后即会运行，周期未设置也会运行），关闭后将仅按照刮削周期运行（同时上次触发运行的任务如果在运行中也会停止）',
                            'type': 'switch',
                            'id': 'onlyonce',
                        }
                    ]
                ]
            }
        ]

    def init_config(self, config=None):
        self.downloader = Downloader()
        self.message = Message()
        # 读取配置
        if config:
            self._enable = config.get("enable")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._downloaders = config.get("downloaders")
        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=Config().get_timezone())
            if self._cron:
                self.info(f"标记服务启动，周期：{self._cron}")
                self._scheduler.add_job(self.auto_mark,
                                        CronTrigger.from_crontab(self._cron))
            if self._onlyonce:
                self.info(f"标记服务启动，立即运行一次")
                self._scheduler.add_job(self.auto_mark, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(Config().get_timezone())))
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "enable": self._enable,
                    "onlyonce": self._onlyonce,
                    "cron": self._cron,
                    "downloaders": self._downloaders
                })
            if self._cron or self._onlyonce:
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self):
        return True if self._enable and self._cron and self._downloaders else False

    def auto_mark(self):
        """
        开始标记
        """
        if not self._enable or not self._downloaders:
            self.warn("标记服务未启用或未配置")
            return
        # 扫描下载器辅种
        for downloader in self._downloaders:
            self.info(f"开始扫描下载器：{downloader} ...")
            # 下载器类型
            downloader_type = self.downloader.get_downloader_type(downloader_id=downloader)
            # 获取下载器中已完成的种子
            torrents = self.downloader.get_completed_torrents(downloader_id=downloader)
            if torrents:
                self.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")
            else:
                self.info(f"下载器 {downloader} 没有已完成种子")
                continue
            for torrent in torrents:
                if self._event.is_set():
                    self.info(f"标记服务停止")
                    return
                # 获取种子hash
                hash_str = self.__get_hash(torrent, downloader_type)
                # 获取种子标签
                torrent_tags = self.__get_tag(torrent, downloader_type).split(",")
                pt_flag = self.__isPT(torrent, downloader_type)

                if pt_flag is True:
                    torrent_tags.append("PT")
                    self.downloader.set_torrents_tag(downloader_id=downloader, ids=hash_str, tags=torrent_tags)
                else:
                    torrent_tags.append("BT")
                    self.downloader.set_torrents_tag(downloader_id=downloader, ids=hash_str, tags=torrent_tags)
        self.info("标记任务执行完成")

    @staticmethod
    def __get_hash(torrent, dl_type):
        """
        获取种子hash
        """
        try:
            return torrent.get("hash") if dl_type == DownloaderType.QB else torrent.hashString
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_tag(torrent, dl_type):
        """
        获取种子标签
        """
        try:
            return torrent.get("tags") or [] if dl_type == DownloaderType.QB else torrent.labels or []
        except Exception as e:
            print(str(e))
            return []
   
    @staticmethod
    def __isPT(torrent, dl_type):
        """
        获取种子标签
        """
        try:
            tracker_list = list()
            if dl_type == DownloaderType.QB and torrent.trackers_count == 1:
                tracker_list.append(torrent.tracker)
            elif dl_type == DownloaderType.TR:
                tracker_list = torrent.tracker_list or []
            if len(tracker_list) == 1:
                if tracker_list[0].find("secure=") != -1 \
                    or tracker_list[0].find("passkey=") != -1 \
                    or tracker_list[0].find("totheglory") != -1:
                    return True
            else:
                return False
        except Exception as e:
            print(str(e))
            return False

    # @staticmethod
    # def __get_save_path(torrent, dl_type):
    #     """
    #     获取种子保存路径
    #     """
    #     try:
    #         return torrent.get("save_path") if dl_type == DownloaderType.QB else torrent.download_dir
    #     except Exception as e:
    #         print(str(e))
    #         return ""

    # def __get_download_url(self, seed, site, base_url):
    #     """
    #     拼装种子下载链接
    #     """

    #     def __is_special_site(url):
    #         """
    #         判断是否为特殊站点
    #         """
    #         if "hdchina.org" in url:
    #             return True
    #         if "hdsky.me" in url:
    #             return True
    #         if "hdcity.in" in url:
    #             return True
    #         if "totheglory.im" in url:
    #             return True
    #         return False

    #     try:
    #         if __is_special_site(site.get('strict_url')):
    #             # 从详情页面获取下载链接
    #             return self.__get_torrent_url_from_page(seed=seed, site=site)
    #         else:
    #             download_url = base_url.replace(
    #                 "id={}",
    #                 "id={id}"
    #             ).replace(
    #                 "/{}",
    #                 "/{id}"
    #             ).format(
    #                 **{
    #                     "id": seed.get("torrent_id"),
    #                     "passkey": site.get("passkey") or '',
    #                     "uid": site.get("uid") or ''
    #                 }
    #             )
    #             if download_url.count("{"):
    #                 self.warn(f"当前不支持该站点的辅助任务，Url转换失败：{seed}")
    #                 return None
    #             download_url = re.sub(r"[&?]passkey=", "",
    #                                   re.sub(r"[&?]uid=", "",
    #                                          download_url,
    #                                          flags=re.IGNORECASE),
    #                                   flags=re.IGNORECASE)
    #             return f"{site.get('strict_url')}/{download_url}"
    #     except Exception as e:
    #         self.warn(f"当前不支持该站点的辅助任务，Url转换失败：{str(e)}")
    #         return None

    # def __get_torrent_url_from_page(self, seed, site):
    #     """
    #     从详情页面获取下载链接
    #     """
    #     try:
    #         page_url = f"{site.get('strict_url')}/details.php?id={seed.get('torrent_id')}&hit=1"
    #         self.info(f"正在获取种子下载链接：{page_url} ...")
    #         res = RequestUtils(
    #             cookies=site.get("cookie"),
    #             headers=site.get("ua"),
    #             proxies=Config().get_proxies() if site.get("proxy") else None
    #         ).get_res(url=page_url)
    #         if res is not None and res.status_code in (200, 500):
    #             if "charset=utf-8" in res.text or "charset=UTF-8" in res.text:
    #                 res.encoding = "UTF-8"
    #             else:
    #                 res.encoding = res.apparent_encoding
    #             if not res.text:
    #                 self.warn(f"获取种子下载链接失败，页面内容为空：{page_url}")
    #                 return None
    #             # 使用xpath从页面中获取下载链接
    #             html = etree.HTML(res.text)
    #             for xpath in self._torrent_xpaths:
    #                 download_url = html.xpath(xpath)
    #                 if download_url:
    #                     download_url = download_url[0]
    #                     self.info(f"获取种子下载链接成功：{download_url}")
    #                     if not download_url.startswith("http"):
    #                         if download_url.startswith("/"):
    #                             download_url = f"{site.get('strict_url')}{download_url}"
    #                         else:
    #                             download_url = f"{site.get('strict_url')}/{download_url}"
    #                     return download_url
    #             self.warn(f"获取种子下载链接失败，未找到下载链接：{page_url}")
    #             return None
    #         else:
    #             return None
    #     except Exception as e:
    #         self.warn(f"获取种子下载链接失败：{str(e)}")
    #         return None

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
