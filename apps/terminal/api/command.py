# -*- coding: utf-8 -*-
#
import time
from django.conf import settings
from django.utils import timezone
from django.shortcuts import HttpResponse
from rest_framework import generics
from rest_framework.fields import DateTimeField
from rest_framework.response import Response
from django.template import loader

from terminal.models import CommandStorage, Session
from terminal.filters import CommandFilter
from orgs.utils import current_org
from common.permissions import IsOrgAdminOrAppUser, IsOrgAuditor, IsAppUser
from common.drf.api import JMSBulkModelViewSet
from common.utils import get_logger
from terminal.serializers import InsecureCommandAlertSerializer
from terminal.exceptions import StorageInvalid
from ..backends import (
    get_command_storage, get_multi_command_storage,
    SessionCommandSerializer,
)
from ..notifications import CommandAlertMessage

logger = get_logger(__name__)
__all__ = ['CommandViewSet', 'CommandExportApi', 'InsecureCommandAlertAPI']


class CommandQueryMixin:
    command_store = get_command_storage()
    permission_classes = [IsOrgAdminOrAppUser | IsOrgAuditor]
    filterset_fields = [
        "asset", "system_user", "user", "session", "risk_level",
        "input"
    ]
    default_days_ago = 5

    @staticmethod
    def get_org_id():
        if current_org.is_default():
            org_id = ''
        else:
            org_id = current_org.id
        return org_id

    def get_query_risk_level(self):
        risk_level = self.request.query_params.get('risk_level')
        if risk_level is None:
            return None
        if risk_level.isdigit():
            return int(risk_level)
        return None

    def get_queryset(self):
        # 解决访问 /docs/ 问题
        if hasattr(self, 'swagger_fake_view'):
            return self.command_store.model.objects.none()
        date_from, date_to = self.get_date_range()
        q = self.request.query_params
        multi_command_storage = get_multi_command_storage()
        queryset = multi_command_storage.filter(
            date_from=date_from, date_to=date_to,
            user=q.get("user"), asset=q.get("asset"), system_user=q.get("system_user"),
            input=q.get("input"), session=q.get("session_id", q.get('session')),
            risk_level=self.get_query_risk_level(), org_id=self.get_org_id(),
        )
        return queryset

    def filter_queryset(self, queryset):
        # 解决es存储命令时，父类根据filter_fields过滤出现异常的问题，返回的queryset类型list
        return queryset

    def get_date_range(self):
        now = timezone.now()
        days_ago = now - timezone.timedelta(days=self.default_days_ago)
        date_from_st = days_ago.timestamp()
        date_to_st = now.timestamp()

        query_params = self.request.query_params
        date_from_q = query_params.get("date_from")
        date_to_q = query_params.get("date_to")

        dt_parser = DateTimeField().to_internal_value

        if date_from_q:
            date_from_st = dt_parser(date_from_q).timestamp()

        if date_to_q:
            date_to_st = dt_parser(date_to_q).timestamp()
        return date_from_st, date_to_st


class CommandViewSet(JMSBulkModelViewSet):
    """接受app发送来的command log, 格式如下
    {
        "user": "admin",
        "asset": "localhost",
        "system_user": "web",
        "session": "xxxxxx",
        "input": "whoami",
        "output": "d2hvbWFp",  # base64.b64encode(s)
        "timestamp": 1485238673.0
    }

    """
    command_store = get_command_storage()
    permission_classes = [IsOrgAdminOrAppUser | IsOrgAuditor]
    serializer_class = SessionCommandSerializer
    filterset_class = CommandFilter
    ordering_fields = ('timestamp', )

    def merge_all_storage_list(self, request, *args, **kwargs):
        merged_commands = []

        storages = CommandStorage.objects.all()
        for storage in storages:
            if not storage.is_valid():
                continue

            qs = storage.get_command_queryset()
            commands = self.filter_queryset(qs)
            merged_commands.extend(commands[:])  # ES 默认只取 10 条数据
        order = self.request.query_params.get('order', None)
        if order == 'timestamp':
            merged_commands.sort(key=lambda command: command.timestamp)
        else:
            merged_commands.sort(key=lambda command: command.timestamp, reverse=True)
        page = self.paginate_queryset(merged_commands)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(merged_commands, many=True)
        return Response(serializer.data)

    def list(self, request, *args, **kwargs):
        command_storage_id = self.request.query_params.get('command_storage_id')
        session_id = self.request.query_params.get('session_id')

        if session_id and not command_storage_id:
            # 会话里的命令列表肯定会提供 session_id，这里防止 merge 的时候取全量的数据
            return self.merge_all_storage_list(request, *args, **kwargs)

        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            page = self.load_remote_addr(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        # 适配像 ES 这种没有指定分页只返回少量数据的情况
        queryset = queryset[:]

        queryset = self.load_remote_addr(queryset)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def load_remote_addr(self, queryset):
        commands = list(queryset)
        session_ids = {command.session for command in commands}
        sessions = Session.objects.filter(id__in=session_ids).values_list('id', 'remote_addr')
        session_addr_map = {str(i): addr for i, addr in sessions}
        for command in commands:
            command.remote_addr = session_addr_map.get(command.session, '')
        return commands

    def get_queryset(self):
        command_storage_id = self.request.query_params.get('command_storage_id')
        storage = CommandStorage.objects.get(id=command_storage_id)
        if not storage.is_valid():
            raise StorageInvalid
        else:
            qs = storage.get_command_queryset()
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, many=True)
        if serializer.is_valid():
            ok = self.command_store.bulk_save(serializer.validated_data)
            if ok:
                return Response("ok", status=201)
            else:
                return Response("Save error", status=500)
        else:
            msg = "Command not valid: {}".format(serializer.errors)
            logger.error(msg)
            return Response({"msg": msg}, status=401)


class CommandExportApi(CommandQueryMixin, generics.ListAPIView):
    serializer_class = SessionCommandSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        template = 'terminal/command_report.html'
        context = {
            'queryset': queryset,
            'total_count': len(queryset),
            'now': time.time(),
        }
        content = loader.render_to_string(template, context, request)
        content_type = 'application/octet-stream'
        response = HttpResponse(content, content_type)
        filename = 'command-report-{}.html'.format(int(time.time()))
        response['Content-Disposition'] = 'attachment; filename="%s"' % filename
        return response


class InsecureCommandAlertAPI(generics.CreateAPIView):
    permission_classes = [IsAppUser]
    serializer_class = InsecureCommandAlertSerializer

    def post(self, request, *args, **kwargs):
        serializer = InsecureCommandAlertSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        commands = serializer.validated_data
        for command in commands:
            if command['risk_level'] >= settings.SECURITY_INSECURE_COMMAND_LEVEL:
                CommandAlertMessage(command).publish_async()
        return Response()
