"""Уведомление об ожидающем задании от установленного приложения macOS."""

import objc
from Foundation import NSBundle, NSObject
from PyObjCTools import AppHelper
from UserNotifications import (
    UNAuthorizationOptionAlert,
    UNMutableNotificationContent,
    UNNotificationDefaultActionIdentifier,
    UNNotificationPresentationOptionBanner,
    UNNotificationPresentationOptionList,
    UNNotificationRequest,
    UNUserNotificationCenter,
)

from .macos_autostart import LABEL

WAITING = "waiting-for-answer"


class StudioAgentNotificationDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, complete):
        complete(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList)

    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, center, response, complete):
        try:
            if (response.actionIdentifier() == UNNotificationDefaultActionIdentifier
                    and response.notification().request().identifier() == WAITING):
                self.post("studio")  # очередь Tk принимает команды и из потока Cocoa
        finally:
            complete()


class WaitNotifications:
    def __init__(self, center, post):
        self._center = center
        self._delegate = StudioAgentNotificationDelegate.alloc().init()  # у центра слабая ссылка
        self._delegate.post = post
        center.setDelegate_(self._delegate)
        self._closed = False

    @classmethod
    def for_app(cls, post):
        # У голого python нет идентичности Studio Agent. Не обращаемся к системному центру из него.
        if NSBundle.mainBundle().bundleIdentifier() == LABEL:
            return cls(UNUserNotificationCenter.currentNotificationCenter(), post)
        return None

    def show(self, title, body):
        def authorized(granted, error):
            if granted:
                AppHelper.callAfter(self._deliver, title, body)

        self._center.requestAuthorizationWithOptions_completionHandler_(UNAuthorizationOptionAlert, authorized)

    def _deliver(self, title, body):
        if self._closed:
            return
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(WAITING, content, None)
        self._center.addNotificationRequest_withCompletionHandler_(request, None)

    def close(self):
        self._closed = True
        self._center.removePendingNotificationRequestsWithIdentifiers_([WAITING])
        self._center.removeDeliveredNotificationsWithIdentifiers_([WAITING])
        self._center.setDelegate_(None)
