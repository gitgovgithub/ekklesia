# -*- coding: utf-8 -*-
#
# Backend views
#
# Copyright (C) 2013,2014 by entropy@heterarchy.net
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# For more details see the file COPYING.

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
User = get_user_model()
from django.core.urlresolvers import reverse

from django.contrib.auth.models import Group

from idapi.models import Message, PublicKey
from django.conf import settings

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from rest_framework import exceptions
from idapi.authentication import SSLBasicAuthentication

class MembersLogin(SSLBasicAuthentication):
    www_authenticate_realm = 'members'

class InvitationsLogin(SSLBasicAuthentication):
    www_authenticate_realm = 'invitations'

class KeysLogin(SSLBasicAuthentication):
    www_authenticate_realm = 'keys'

def api_crypto(api):
    from idapi.mails import gnupg_import_init
    from kryptomime.pgp import GPGMIME
    gpg = gnupg_import_init(verbose=False)
    sender = getattr(settings, 'API_GNUPG_KEY')
    receiver = getattr(settings, 'API_BACKEND_KEYS', {}).get(api,False)
    crypto = GPGMIME(gpg,default_key=sender)
    decrypt = sender if sender else False
    encrypt = [receiver] if receiver else False
    return crypto, receiver, decrypt, bool(sender), encrypt

def get_members(crypto=True):
    from ekklesia.data import DataTable
    from accounts.models import Account, Invitation
    if crypto:
        gpg, verify, decrypt, sign, encrypt = api_crypto('members')
        if crypto==True: crypto = gpg # not debug
    else: verify = decrypt = sign = encrypt = False
    columns = ['uuid']
    twofactor = getattr(settings, 'TWO_FACTOR_SIGNUP')
    if twofactor: columns.append('activate')
    writer = DataTable(columns,gpg=crypto,
        dataformat='member',fileformat='json',version=[1,0])
    writer.open(mode='w',encrypt=encrypt,sign=sign)
    members = Account.objects.exclude(uuid=None)
    members = members.filter(email_unconfirmed=None, # only confirmed emails
         status__in=(Account.MEMBER,Account.ELIGIBLE,Account.NEWMEMBER))
    for member in members.values('uuid','status'):
        if twofactor:
            if member['status'] == Account.NEWMEMBER:
                try: inv = Invitation.objects.get(
                    uuid=member['uuid'],status=Invitation.REGISTERING)
                except Invitation.DoesNotExist: continue
                member['activate'] = inv.secret
            else: member['activate'] = ''
        writer.write(member)
    return writer.close()

def update_members(members,departments,crypto=True):
    from ekklesia.data import DataTable
    from idapi.mails import gnupg_import_init
    from accounts.models import Account, NestedGroup, Invitation
    import six
    if crypto:
        gpg, verify, decrypt, sign, encrypt = api_crypto('members')
        if crypto==True: crypto = gpg # not debug
    else: verify = decrypt = sign = encrypt = False
    deldeps = set(NestedGroup.objects.exclude(syncid=None).values_list('syncid',flat=True))
    reader = DataTable(['id','parent','name','depth'],gpg=crypto,remap={'id':'syncid'},
        dataformat='department',fileformat='json',version=[1,0])
    reader.open(departments,'r',encrypt=decrypt,sign=verify)
    newdeps = set() # check duplicate
    deps = []
    # FIXME also work with name ids
    for dep in reader:
        syncid = dep['syncid']
        if syncid in newdeps: raise KeyError
        newdeps.add(syncid)
        deps.append(dep)
    # check whether all parents exist
    for dep in deps:
        parent = dep['parent']
        if parent and not parent in newdeps:
            #print parent,'missing'
            raise KeyError # parent missing

    columns = ['uuid','status','verified','department']
    required = ['uuid','status']
    twofactor = getattr(settings, 'TWO_FACTOR_SIGNUP')
    if twofactor:
        columns.append('activate')
        required.append('activate')
    reader = DataTable(columns, required=required,gpg=crypto,
        dataformat='member',fileformat='json',version=[1,0])
    reader.open(members,'r',encrypt=decrypt,sign=verify)
    newmembers = set() # check duplicate
    stati = {'deleted':Account.DELETED,'member':Account.MEMBER,'eligible':Account.ELIGIBLE}
    data = []
    for member in reader:
        uuid = member['uuid']
        if uuid in newmembers: raise KeyError
        newmembers.add(uuid)
        status = stati.get(member['status'])
        assert not status is None, "invalid status"
        member['status'] = status
        if status == Account.DELETED:
            member['is_active'] = False
        data.append(member)

    done = set()
    while len(deps):
        todo = []
        for dep in deps:
            parent = dep['parent']
            if parent:
                if not parent in done:
                    todo.append(dep)
                    continue
                parent = NestedGroup.objects.get(syncid=parent)
            dep['parent'] = parent
            done.add(dep['syncid'])
            try: obj = NestedGroup.objects.get(syncid=dep['syncid'])
            except NestedGroup.DoesNotExist:
                NestedGroup.objects.create(**dep)
                continue
            deldeps.discard(obj.syncid) # keep later
            if obj._dict == dep: continue
            for k,v in six.iteritems(dep): setattr(obj,k,v)
            obj.save()
        deps = todo

    for member in data:
        uuid = member['uuid']
        try: obj = Account.objects.get(uuid=uuid)
        except Account.DoesNotExist: continue
        try: 
            if obj.email_unconfirmed: continue # ignore unconfirmed email
        except AttributeError: pass
        if obj.status==Account.NEWMEMBER:
            try: inv = Invitation.objects.get(uuid=uuid)
            except Invitation.DoesNotExist: inv = None
            assert inv and inv.status==Invitation.REGISTERING, "invalid state for newmember"
            if member['status'] == Account.DELETED:
                obj.delete()
                inv.delete()
                continue
            # check whether activation failed or to be deleted
            if twofactor and member['activate'] != True:
                inv.status = Invitation.FAILED
                inv.save()
                obj.delete()
                continue
            inv.status = Invitation.REGISTERED
            inv.save()
        if 'department' in member:
            dep = member['department']
            del member['department']
            if not dep:
                obj.nested_groups = NestedGroup.objects.filter(syncid=None,account=obj)
            elif not obj.nested_groups.filter(syncid=dep).exists(): # already in dep?
                ngroups = NestedGroup.objects.filter(syncid=None,account=obj) | \
                    NestedGroup.objects.filter(syncid=dep) # union existing ngroups + dep
                obj.nested_groups = ngroups
        for k,v in six.iteritems(member): setattr(obj,k,v)
        if obj.has_changed: obj.save()

    for syncid in deldeps:
        NestedGroup.objects.get(syncid=syncid).delete()
    return 'ok'

class MembersView(APIView):
    authentication_classes = (MembersLogin,)
    permission_classes = ()
    parser_classes = (JSONParser,)

    def get(self, request, format=None):
        return Response(get_members())

    def post(self, request, format=None):
        members = request.DATA['members']
        departments = request.DATA['departments']
        ok = update_members(members,departments)
        return Response({'status': ok})

def get_invitations(crypto=True):
    from ekklesia.data import DataTable
    from idapi.mails import gnupg_init
    from accounts.models import Invitation
    if crypto:
        gpg, verify, decrypt, sign, encrypt = api_crypto('invitations')
        if crypto==True: crypto = gpg # not debug
    else: verify = decrypt = sign = encrypt = False
    writer = DataTable(['uuid','status'],gpg=crypto,
        dataformat='invitation',fileformat='json',version=[1,0])
    writer.open(mode='w',encrypt=encrypt,sign=sign)
    count = 0
    invs = Invitation.objects.exclude(status=Invitation.DELETED)
    stati = {Invitation.REGISTERED:'registered',Invitation.REGISTERING:'new',
        Invitation.FAILED:'failed',Invitation.NEW:'new'}
    for inv in invs.values('uuid','status'):
        inv['status'] = stati[inv['status']]
        writer.write(inv)
        count += 1
    return writer.close()

def update_invitations(invitations,crypto=True):
    from ekklesia.data import DataTable
    from idapi.mails import gnupg_import_init
    from accounts.models import Account, Invitation
    import six
    delete_implicit = getattr(settings, 'INVITATIONS_DELETE_IMPLICT', False)
    if crypto:
        gpg, verify, decrypt, sign, encrypt = api_crypto('invitations')
        if crypto==True: crypto = gpg # not debug
    else: verify = decrypt = sign = encrypt = False
    if delete_implicit:
        delinvs = set(Invitation.objects.values_list('uuid',flat=True))
    else: delinvs = []
    reader = DataTable(['uuid','status','code'],gpg=crypto,
        dataformat='invitation',fileformat='json',version=[1,0])
    reader.open(invitations,'r',encrypt=decrypt,sign=verify)
    newinvs = set() # check duplicate
    data = []
    stati = {'new':Invitation.NEW,'deleted':Invitation.DELETED,
        'failed':Invitation.FAILED,'registered':Invitation.REGISTERED}
    for inv in reader:
        uuid = inv['uuid']
        if uuid in newinvs: raise KeyError
        newinvs.add(uuid)
        inv['status'] = stati[inv['status']]
        data.append(inv)

    for inv in data:
        uuid = inv['uuid']
        if delete_implicit: delinvs.discard(uuid)
        try: obj = Invitation.objects.get(uuid=uuid)
        except Invitation.DoesNotExist:
            if inv['status']==Invitation.NEW: Invitation.objects.create(**inv)
            continue
        if inv['status'] != Invitation.NEW:
            obj.delete()
            continue
        for k,v in six.iteritems(inv): setattr(obj,k,v)
        if obj.has_changed: obj.save()

    for uuid in delinvs:
        Invitation.objects.get(uuid=uuid).delete()

    return 'ok'

class InvitationsView(APIView):
    authentication_classes = (InvitationsLogin,)
    permission_classes = ()
    parser_classes = (JSONParser,)

    def get(self, request, format=None):
        invitations = get_invitations()
        return Response(invitations)

    def post(self, request, format=None):
        ok = update_invitations(request.DATA)
        return Response({'status': ok})

class KeysView(APIView):
    authentication_classes = (KeysLogin,)
    permission_classes = ()
    parser_classes = (JSONParser, FormParser, MultiPartParser)

    def get(self, request, format=None): pass

    def post(self, request, format=None): pass
