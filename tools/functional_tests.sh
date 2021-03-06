#!/bin/bash

# Copyright (c) 2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Globals

cleanup()
{
        sudo service memcached stop
        sudo swift-init main stop
		sudo pip uninstall -y gluster-swift
        sudo rm -rf /etc/swift > /dev/null 2>&1
        sudo rm -rf /mnt/gluster-object/test{,2}/* > /dev/null 2>&1
        sudo setfattr -x user.swift.metadata /mnt/gluster-object/test{,2} > /dev/null 2>&1
}

quit()
{
        echo "$1"
        exit 1
}


fail()
{
        cleanup
	quit "$1"
}

### MAIN ###

# Only run if there is no configuration in the system
if [ -x /etc/swift ] ; then
	quit "/etc/swift exists, cannot run functional tests."
fi

# Check the directories exist
DIRS="/mnt/gluster-object /mnt/gluster-object/test /mnt/gluster-object/test2"
for d in $DIRS ; do
	if [ ! -x $d ] ; then
		quit "$d must exist on an XFS or GlusterFS volume"
	fi
done

export SWIFT_TEST_CONFIG_FILE=/etc/swift/test.conf

# Download and cache swift
pip install --no-install --download-cache=$HOME/.pipcache swift==1.9.1
# Install swift
sudo pip install --download-cache=$HOME/.pipcache swift==1.9.1
# Install gluster-swift
sudo pip install -e $PWD

# Install the configuration files
sudo mkdir /etc/swift > /dev/null 2>&1
sudo cp -r test/functional/conf/* /etc/swift || fail "Unable to copy configuration files to /etc/swift"
( cd /etc/swift ; sudo gluster-swift-gen-builders test test2 ) || fail "Unable to create ring files"

# Start the services
sudo service memcached start || fail "Unable to start memcached"
sudo swift-init main start || fail "Unable to start swift"

mkdir functional_tests > /dev/null 2>&1
nosetests -v --exe \
	--with-xunit \
	--xunit-file functional_tests/gluster-swift-functional-TC-report.xml test/functional || fail "Functional tests failed"
nosetests -v --exe \
	--with-xunit \
	--xunit-file functional_tests/gluster-swift-functionalnosetests-TC-report.xml test/functionalnosetests || fail "Functional-nose tests failed"

cleanup
exit 0
