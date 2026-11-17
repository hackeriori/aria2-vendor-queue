#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alist_client.py —— Alist API 的最小封装（仅标准库）

用途：
    1. 登录拿 JWT（Alist 的 /d/ 直链必须带 sign，sign 只能由 Alist 自己生成）；
    2. fs/list + refresh 强制刷新目录缓存（刚转存进来的文件 Alist 看不到）；
    3. fs/get 取 raw_url，交给 aria2 下载。

token 失效（Alist 默认 48h）时自动重新登录一次。
"""

import json
import time
import urllib.error
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
