#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alist_client.py —— Alist API 的最小封装（仅标准库）

用途：
    1. 登录拿 JWT（Alist 的 /d/ 直链必须带 sign，sign 只能由 Alist 自己生成）；
    2. fs/list + refresh 强制刷新目录缓存（刚转存进来的文件 Alist 看不到）；
    3. fs/get 取 raw_url，交给 aria2 下载；
    4. storage_cookie：从 Alist 的存储配置里取网盘 cookie（夸克 cookie 只在 Alist 维护一份）。

token 失效（Alist 默认 48h）时自动重新登录一次。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class AlistError(Exception):
    pass


class AlistClient:
    def __init__(self, base_url, username, password, timeout=20, logger=None):
        self.base = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.log = logger or (lambda msg: None)
        self.token = ""
        self._refreshed = {}   # 目录 -> 上次刷新时间，避免同一轮重复刷
        self.refresh_ttl = 25

    # ---------- 底层 ----------

    def _post(self, path, body, auth=True):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                raise AlistError("HTTP %s %s: %s" % (e.code, path, raw[:200]))
        except urllib.error.URLError as e:
            raise AlistError("%s 网络错误: %s" % (path, e))

    def _get(self, path, params=None, auth=True):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {}
        if auth and self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                raise AlistError("HTTP %s %s: %s" % (e.code, path, raw[:200]))
        except urllib.error.URLError as e:
            raise AlistError("%s 网络错误: %s" % (path, e))

    def login(self):
        resp = self._post("/api/auth/login",
                          {"username": self.username, "password": self.password}, auth=False)
        if resp.get("code") != 200:
            raise AlistError("Alist 登录失败: %s %s" % (resp.get("code"), resp.get("message")))
        self.token = (resp.get("data") or {}).get("token") or ""
        if not self.token:
            raise AlistError("Alist 登录未返回 token")
        return self.token

    def _call(self, path, body):
        if not self.token:
            self.login()
        resp = self._post(path, body)
        if resp.get("code") == 401:
            self.log("Alist token 失效，重新登录")
            self.login()
            resp = self._post(path, body)
        if resp.get("code") != 200:
            raise AlistError("%s 失败: %s %s" % (path, resp.get("code"), resp.get("message")))
        return resp.get("data")

    def _call_get(self, path, params=None):
        if not self.token:
            self.login()
        resp = self._get(path, params)
        if resp.get("code") == 401:
            self.log("Alist token 失效，重新登录")
            self.login()
            resp = self._get(path, params)
        if resp.get("code") != 200:
            raise AlistError("%s 失败: %s %s" % (path, resp.get("code"), resp.get("message")))
        return resp.get("data")

    # ---------- 存储配置 ----------

    def storage_cookie(self, mount_path=None, driver="Quark"):
        """从 Alist 的存储配置里取出网盘 cookie（需要 Alist 管理员账号）。

        Alist 把每个存储的 addition 原样存库（夸克 driver 的 cookie 是明文），
        管理接口 GET /api/admin/storage/list 会把 addition 一起返回。
        这样夸克 cookie 只需要在 Alist 后台维护一份，本程序启动/失效时现取。
        """
        try:
            data = self._call_get("/api/admin/storage/list", {"page": 1, "per_page": 200})
        except AlistError as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "没有权限" in msg or "not admin" in msg.lower():
                raise AlistError("读取存储配置需要 Alist 管理员账号"
                                 "（config.quark.alist.username 当前不是管理员）: %s" % msg)
            raise

        items = []
        if isinstance(data, dict):
            items = data.get("content") or []
        elif isinstance(data, list):
            items = data
        cands = [s for s in items if (s.get("driver") or "") == driver]
        if not cands:
            raise AlistError("Alist 里没找到 driver=%s 的存储" % driver)

        want = (mount_path or "").rstrip("/")
        exact = [s for s in cands if (s.get("mount_path") or "").rstrip("/") == want]
        if exact:
            pick = exact[0]
        elif len(cands) == 1:
            pick = cands[0]
        else:
            raise AlistError("Alist 里有多份 %s 存储，请用 alistMountPath 指明一份，"
                             "候选项: %s" % (driver, ", ".join(s.get("mount_path") or "?" for s in cands)))

        add = pick.get("addition")
        if isinstance(add, str):
            try:
                add = json.loads(add)
            except Exception:
                raise AlistError("存储 %s 的 addition 不是合法 JSON" % (pick.get("mount_path") or "?"))
        cookie = ((add or {}).get("cookie") or "").strip()
        if not cookie:
            raise AlistError("Alist 存储 %s 里没有填 cookie" % (pick.get("mount_path") or "?"))
        return cookie

    # ---------- 对外 ----------

    def refresh_dir(self, path):
        """强制刷新 path 的父目录及其所有祖先的缓存。

        Alist 对网盘目录是分层缓存的：刚转存进来的文件，只刷新叶子父目录不够，
        祖先目录（如 /夸克/来自：分享）的缓存还是旧的，fs/get 就会报
        "failed get parent list: object not found"。所以从根往下逐级刷新。
        """
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for seg in parts[:-1]:
            cur += "/" + seg
            now = time.time()
            if now - self._refreshed.get(cur, 0) < self.refresh_ttl:
                continue
            try:
                self._call("/api/fs/list", {"path": cur, "page": 1, "per_page": 200,
                                            "refresh": True, "password": ""})
                self._refreshed[cur] = now
            except AlistError as e:
                self.log("刷新目录缓存失败（忽略）%s: %s" % (cur, e))

    def get_raw_url(self, path):
        """取文件直链（带 sign），可直接交给 aria2。"""
        self.refresh_dir(path)
        data = self._call("/api/fs/get", {"path": path, "password": ""})
        raw = (data or {}).get("raw_url") or ""
        if not raw:
            raise AlistError("取直链失败（可能是 Alist 还没看到该文件）: %s" % path)
        return raw

    def exists(self, path):
        try:
            data = self._call("/api/fs/get", {"path": path, "password": ""})
            return bool((data or {}).get("raw_url"))
        except AlistError:
            return False
