# Generated by Django 4.1.4 on 2022-12-18 18:47

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0003_auto_20220329_2107'),
    ]

    operations = [
        migrations.AddField(
            model_name='cronjob',
            name='at_front',
            field=models.BooleanField(default=False, verbose_name='At front'),
        ),
        migrations.AddField(
            model_name='repeatablejob',
            name='at_front',
            field=models.BooleanField(default=False, verbose_name='At front'),
        ),
        migrations.AddField(
            model_name='scheduledjob',
            name='at_front',
            field=models.BooleanField(default=False, verbose_name='At front'),
        ),
    ]
