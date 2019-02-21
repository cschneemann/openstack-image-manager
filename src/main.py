import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

from natsort import natsorted
from oslo_config import cfg
import openstack
import os_client_config
import requests
import yaml

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO, datefmt='%Y-%m-%d %H:%M:%S')

PROJECT_NAME='images'
CONF = cfg.CONF
opts = [
  cfg.StrOpt('cloud', help='Cloud name in clouds.yaml', default='images'),
  cfg.StrOpt('images', help='Path to the images.yml file', default='etc/images.yml')
]
CONF.register_cli_opts(opts)
CONF(sys.argv[1:], project=PROJECT_NAME)

REQUIRED_KEYS = [
    'format',
    'name',
    'status',
    'versions',
    'visibility',
]

with open(CONF.images) as fp:
    data = yaml.load(fp)
    images = data.get('images', [])

conn = openstack.connect(cloud=CONF.cloud)
glance = os_client_config.make_client("image", cloud=CONF.cloud)

def create_import_task(glance, image, url):
    logging.info("Creating import task '%s'" % name)

    input = {
        'import_from_format': image['format'],
        'import_from': url,
        'image_properties': {
            'container_format': 'bare',
            'disk_format': image['format'],
            'min_disk': image.get('min_disk', 0),
            'min_ram': image.get('min_ram', 0),
            'name': name,
            'visibility': 'private'
        }
    }

    t = glance.tasks.create(type='import', input=input)

    while True:
        try:
            status = glance.tasks.get(t.id).status
            if status not in ['failure', 'success']:
                logging.info("Waiting for task %s" % t.id)
                time.sleep(10.0)
            else:
                break

        except:
            time.sleep(5.0)
            pass

    return status

def get_images(conn):
    result = {}

    for image in conn.list_images():
        if 'managed_by_betacloud' in image.tags and (image.is_public or image.owner == conn.current_project_id):
            result[image.name] = image
        else:
            logging.debug("'%s' not managed" % image.name)

    return result

cloud_images = get_images(conn)

# manage existing images and add new ones

for image in images:
    skip = False
    for required_key in REQUIRED_KEYS:
        if required_key not in image:
            logging.error("'%s' lacks the necessary key %s" % (image['name'], required_key))
            skip = True
    if skip:
        continue

    logging.info("Processing '%s'" % image['name'])

    versions = dict()
    for version in image['versions']:
        versions[str(version['version'])] = {
            'url': version['url']
        }

        if 'os_version' in version:
            versions[version['version']]['os_version'] = version['os_version']

    sorted_versions = natsorted(versions.keys())
    image['tags'].append('managed_by_betacloud')

    for version in sorted_versions:
        if image['multi']:
            name = "%s (%s)" % (image['name'], version)
        else:
            name = "%s %s" % (image['name'], version)

        logging.info("Processing image '%s'" % name)

        existence = name in cloud_images

        if image['multi'] and len(sorted_versions) > 1 and version == sorted_versions[-1] and not existence:
            previous = "%s (%s)" % (image['name'], sorted_versions[-2])
            existence = previous in cloud_images and image['name'] in cloud_images
        elif image['multi'] and len(sorted_versions) > 1 and version == sorted_versions[-2] and not existence:
            existence = image['name'] in cloud_images
        elif image['multi'] and len(sorted_versions) == 1:
            existence = image['name'] in cloud_images

        status = None
        if not existence:
            url = versions[version]['url']

            r = requests.head(url)
            logging.info("Tested URL %s: %s" % (url, r.status_code))

            if r.status_code not in [200, 302]:
                logging.warning("Skipping '%s'" % name)
                continue

            status = create_import_task(glance, image, url)

            if status == 'success':
                cloud_images = get_images(conn)

        if image['multi'] and existence and version == sorted_versions[-1] and image['name'] in cloud_images:
            name = image['name']

        if name in cloud_images:
            logging.info("Checking parameters of '%s'" % name)

            cloud_image = cloud_images[name]
            properties = cloud_image.properties
            tags = cloud_image.tags

            if 'min_disk' in image and image['min_disk'] != cloud_image.min_disk:
                logging.info("Setting min_disk: %s != %s" % (image['min_disk'], cloud_image.min_disk))
                glance.images.update(cloud_image.id, **{'min_disk': int(image['min_disk'])})

            if 'min_ram' in image and image['min_ram'] != cloud_image.min_ram:
                logging.info("Setting min_ram: %s != %s" % (image['min_ram'], cloud_image.min_ram))
                glance.images.update(cloud_image.id, **{'min_ram': int(image['min_ram'])})

            if not image['multi']:
                image['meta']['os_version'] = version
            else:
                if 'os_version' in versions[version]:
                    image['meta']['os_version'] = versions[version]['os_version']

            for tag in image['tags']:
                if tag not in tags:
                    logging.info("Adding tag %s" % (tag))
                    glance.image_tags.update(cloud_image.id, tag)

            for tag in tags:
                if tag not in image['tags']:
                    logging.info("Deleting tag %s" % (tag))
                    glance.image_tags.delete(cloud_image.id, tag)

            for property in properties:
                if property in image['meta']:
                    if image['meta'][property] != properties[property]:
                        logging.info("Setting property %s: %s != %s" % (property, properties[property], image['meta'][property]))
                        glance.images.update(cloud_image.id, **{property: str(image['meta'][property])})
                elif property not in ['self', 'schema']:
                    # FIXME: handle deletion of properties
                    logging.info("Deleting property %s" % (property))

            for property in image['meta']:
                if property not in properties:
                    logging.info("Setting property %s: %s" % (property, image['meta'][property]))
                    glance.images.update(cloud_image.id, **{property: str(image['meta'][property])})

            logging.info("Checking status of '%s'" % name)
            if cloud_image.status != image['status'] and image['status'] == 'deactivated':
                logging.info("Deactivating image '%s'" % name)
                glance.images.deactivate(cloud_image.id)
            elif cloud_image.status != image['status'] and image['status'] == 'active':
                logging.info("Reactivating image '%s'" % name)
                glance.images.reactivate(cloud_image.id)

            logging.info("Checking visibility of '%s'" % name)
            if image['multi'] and image['visibility'] == 'public' and version not in sorted_versions[-3:]:
                visibility = 'community'
            else:
                visibility = image['visibility']

            if cloud_image.visibility != visibility:
                logging.info("Set visibility of '%s' to '%s'" % (name, visibility))
                glance.images.update(cloud_image.id, visibility=visibility)

    if image['multi'] and len(sorted_versions) > 1:
        name = image['name']
        latest = "%s (%s)" % (image['name'], sorted_versions[-1])
        current = "%s (%s)" % (image['name'], sorted_versions[-2])

        if name in cloud_images and current not in cloud_images:
            logging.info("Rename %s to %s" % (name, current))
            glance.images.update(cloud_images[name].id, name=current)

        if latest in cloud_images:
            logging.info("Rename %s to %s" % (latest, name))
            glance.images.update(cloud_images[latest].id, name=name)

        cloud_images = get_images(conn)

    elif image['multi'] and len(sorted_versions) == 1:
        name = image['name']
        latest = "%s (%s)" % (image['name'], sorted_versions[-1])

        if latest in cloud_images:
            logging.info("Rename %s to %s" % (latest, name))
            glance.images.update(cloud_images[latest].id, name=name)
